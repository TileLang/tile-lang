import argparse
import itertools
import logging

import tilelang as tl
import tilelang.language as T
from tilelang.autotuner import autotune, jit

# Configure logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def ref_program(A, B):
    """
    A reference matrix multiplication program, used to compare performance.

    Parameters
    ----------
    A : numpy.ndarray
        The matrix with shape (M, K).
    B : numpy.ndarray
        The matrix with shape (N, K).

    Returns
    -------
    np.ndarray
        The result of A @ B.T, shape (M, N).
    """
    return A @ B.T


def get_configs(M, N, K, with_roller=False):
    """
    Generate a list of configuration dictionaries that will be used for tuning.
    
    Parameters
    ----------
    with_roller : bool
        Whether to enable bitblas roller to deduce search spaces

    Returns
    -------
    list of dict
        Each configuration dict includes various block sizes, pipeline stages,
        thread numbers, and other parameters to explore during autotuning.
    """
    if with_roller:
        from bitblas.base.utils import get_roller_hints_from_func
        from bitblas.ops.general_matmul.tirscript import matmul_select_implementation
        from bitblas.base.arch import CUDA
        from bitblas.base.roller.rasterization import NoRasterization
        arch = CUDA("cuda")
        topk = 20

        # Simple TIR Compute Expression
        ir_module = matmul_select_implementation(
            M=M,
            N=N,
            K=K,
            in_dtype="float16",
            out_dtype="float16",
            accum_dtype="float16",
        )

        roller_hints = get_roller_hints_from_func(
            ir_module,
            arch,
            topk,
            tensorcore_only=True,
            allow_gemv=True,
        )

        if roller_hints is None:
            raise ValueError("No Roller Hints Found for TensorCore Scheduling")
        configs = []
        for hint in roller_hints:
            config = {}
            block_m, block_n = hint.block
            warp_m, warp_n = hint.warp
            config["block_M"] = block_m
            config["block_N"] = block_n
            config["block_K"] = hint.rstep[0]
            config["num_stages"] = 0
            config["thread_num"] = (block_m * block_n) // (warp_m * warp_n) * 32
            config["enable_rasteration"] = hint.rasterization_plan is not NoRasterization
            configs.append(config)
        for config in configs:
            print(config)
    else:

        block_M = [64, 128, 256]
        block_N = [64, 128, 256]
        block_K = [32, 64]
        num_stages = [0, 1, 2, 3]
        thread_num = [128, 256]
        enable_rasterization = [True, False]

        _configs = list(
            itertools.product(
                block_M,
                block_N,
                block_K,
                num_stages,
                thread_num,
                enable_rasterization,
            ))

        configs = [
            {
                "block_M": c[0],
                "block_N": c[1],
                "block_K": c[2],
                "num_stages": c[3],
                "thread_num": c[4],
                "enable_rasteration": c[5],  # keep param name for backward-compat
            } for c in _configs
        ]
    return configs


def matmul(M, N, K, with_roller):
    """
    Create an autotuned matrix multiplication kernel for matrices of shape:
      - A: (M, K)
      - B: (N, K)
      - C: (M, N)

    Parameters
    ----------
    M : int
        The dimension M of the matrix multiplication.
    N : int
        The dimension N of the matrix multiplication.
    K : int
        The dimension K of the matrix multiplication.

    Returns
    -------
    (best_latency, best_config, ref_latency)
        best_latency : float
            The best latency found among the tuned configurations.
        best_config : dict
            The parameter configuration that yielded best_latency.
        ref_latency : float
            The baseline latency of the reference program (for computing speedup).
    """

    # Decorate the kernel with autotune & jit, specifying:
    #  - Tuning config list
    #  - Profiling keys
    #  - Warmup and repetition counts for better measurement
    #  - A reference program for correctness verification
    #  - The "tvm" profiler backend
    #  - HIP as the compilation target (modify as needed for your hardware)
    if with_roller:
        # check out bitblas is installed
        try:
            import bitblas  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "BitBlas is not installed. Please install it via 'pip install bitblas'.") from e

    @autotune(
        configs=get_configs(M, N, K, with_roller),
        keys=[
            "block_M",
            "block_N",
            "block_K",
            "num_stages",
            "thread_num",
            "enable_rasteration",
        ],
        warmup=3,
        rep=5,
    )
    @jit(
        out_idx=[2],
        supply_type=tl.TensorSupplyType.Integer,
        ref_prog=ref_program,
        skip_check=True,
        profiler="auto",
        target="auto",
    )
    def kernel(
        block_M=None,
        block_N=None,
        block_K=None,
        num_stages=None,
        thread_num=None,
        enable_rasteration=None,
    ):
        """
        The actual kernel to compute C = A @ B^T.

        Parameters
        ----------
        block_M : int
            Block size in M dimension.
        block_N : int
            Block size in N dimension.
        block_K : int
            Block size in K dimension.
        num_stages : int
            Number of pipelined stages (for asynchronous load).
        thread_num : int
            Number of threads to use per block.
        enable_rasteration : bool
            Whether to enable rasterization (swizzling) optimization.
        k_pack : int
            K dimension packing factor to improve memory coalescing.

        Returns
        -------
        Function
            A TVM Tensor Language function (T.prim_func) that computes matmul.
        """
        # Use half-precision for input data to reduce memory bandwidth,
        # accumulate in float for better numerical accuracy
        dtype = "float16"
        accum_dtype = "float"

        @T.prim_func
        def main(
                A: T.Buffer((M, K), dtype),
                B: T.Buffer((N, K), dtype),
                C: T.Buffer((M, N), dtype),
        ):
            """
            The compiled TVM function for block-level matrix multiplication.

            - We divide the entire (M, N) domain into blocks of shape
              (block_M, block_N).
            - Each block has its own allocated shared memory for sub-blocks
              of A and B.
            - The partial results go into C_local, and then we copy them back
              to global memory C.
            """
            # Bind x-dimension to block index in N,
            #     y-dimension to block index in M.
            with T.Kernel(
                    T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=thread_num) as (bx, by):

                # Allocate shared memory for A sub-block of shape (block_M, block_K)
                A_shared = T.alloc_shared((block_M, block_K), dtype)
                # Allocate shared memory for B sub-block of shape (block_N, block_K)
                B_shared = T.alloc_shared((block_N, block_K), dtype)
                # Allocate a local fragment for intermediate accumulation
                C_local = T.alloc_fragment((block_M, block_N), accum_dtype)

                # Enable (or disable) swizzling optimization
                T.use_swizzle(panel_size=10, enable=enable_rasteration)

                # Clear out the accumulation buffer
                T.clear(C_local)

                # Loop over sub-blocks in K dimension, pipelined by num_stages
                for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
                    # Load a sub-block of A from global memory into A_shared
                    T.copy(
                        A[by * block_M, k * block_K],
                        A_shared,
                    )
                    # Load a sub-block of B from global memory into B_shared
                    T.copy(
                        B[bx * block_N, k * block_K],
                        B_shared,
                    )
                    # Perform a partial matrix multiplication:
                    #   C_local += A_shared @ B_shared^T
                    T.gemm(
                        A_shared,
                        B_shared,
                        C_local,
                        transpose_B=True,
                    )
                # Write back the results from C_local to the global memory C
                T.copy(C_local, C[by * block_M, bx * block_N])

        return main

    return kernel()


if __name__ == "__main__":
    # Parse command-line arguments for matrix dimensions
    parser = argparse.ArgumentParser(description="Autotuned MatMul Benchmark")
    parser.add_argument("--m", type=int, default=8192, help="Matrix dimension M")
    parser.add_argument("--n", type=int, default=8192, help="Matrix dimension N")
    parser.add_argument("--k", type=int, default=8192, help="Matrix dimension K")
    parser.add_argument(
        "--with_roller",
        action="store_true",
        help="Whether to enable BitBLAS roller for search space",
    )
    args = parser.parse_args()

    M, N, K = args.m, args.n, args.k
    with_roller = args.with_roller

    # Compute total floating-point operations to measure throughput
    total_flops = 2 * M * N * K

    # matmul(...) returns (best_latency, best_config, ref_latency)
    best_latency, best_config, ref_latency = matmul(M, N, K, with_roller)

    # Print out the benchmark results
    print(f"Best latency (s): {best_latency}")
    print(f"Best TFlops: {total_flops / best_latency * 1e-9:.3f}")
    print(f"Best config: {best_config}")

    print(f"Reference TFlops: {total_flops / ref_latency * 1e-9:.3f}")