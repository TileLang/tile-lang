# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""The language interface for tl programs."""

from typing import Union, List, Tuple, Optional
from collections import deque
from tvm import tir
from tvm.tir import Var
from tvm.script.ir_builder.tir.frame import TIRFrame
from tvm._ffi import register_object
from tilelang import _ffi_api


class FrameStack:
    """
    A simple stack-like wrapper around a deque that provides
    push, pop, and top methods for convenience.
    """

    def __init__(self):
        self._stack = deque()

    def push(self, item):
        """Pushes an item onto the top of the stack."""
        self._stack.append(item)

    def pop(self):
        """
        Pops and returns the top of the stack, or returns None
        if the stack is empty.
        """
        if self._stack:
            return self._stack.pop()
        raise IndexError(f"{self.__class__.__name__} is empty")

    def top(self):
        """
        Returns the item on the top of the stack without removing it,
        or None if the stack is empty.
        """
        if self._stack:
            return self._stack[-1]
        raise IndexError(f"{self.__class__.__name__} is empty")

    def __len__(self):
        """Returns the number of items in the stack."""
        return len(self._stack)

    def __bool__(self):
        """
        Allows truthy checks on the stack object itself,
        e.g., 'if stack: ...'
        """
        return bool(self._stack)


# Use our new FrameStack instead of a plain list or deque
_kernel_launch_frame_stack = FrameStack()


@register_object("tl.KernelLaunchFrame")
class KernelLaunchFrame(TIRFrame):
    """
    KernelLaunchFrame is a custom TIRFrame that manages block/thread indices
    and handles the entry and exit of the kernel launch scope.
    """

    def __enter__(self) -> Union[Var, List[Var]]:
        """
        Enters the KernelLaunchFrame scope and pushes this frame onto the stack.
        Returns one Var if we detect exactly 5 frames (meaning there is a single
        block dimension), or a list of Vars otherwise.
        """
        super().__enter__()
        _kernel_launch_frame_stack.push(self)

        # If we have exactly 5 frames, return the single iter_var.var.
        if len(self.frames) == 5:
            return self.frames[0].iter_var.var

        # Otherwise, return a list of iter_var.var objects (excluding the last 4 frames).
        return [frame.iter_var.var for frame in self.frames[0:-4]]

    def __exit__(self, ptype, value, trace):
        """
        Exits the KernelLaunchFrame scope and pops this frame from the stack,
        but only if it's indeed the topmost frame.
        """
        # Check if this frame is the current top before popping.
        if _kernel_launch_frame_stack.top() is self:
            _kernel_launch_frame_stack.pop()
        super().__exit__(ptype, value, trace)

    @classmethod
    def Current(cls) -> Optional["KernelLaunchFrame"]:
        """
        Returns the topmost (current) KernelLaunchFrame from the stack if it exists,
        or None if the stack is empty.
        """
        return _kernel_launch_frame_stack.top()

    def get_block_extent(self, dim: int) -> int:
        """
        Returns the block extent for the given dimension.
        dim=0 corresponds to blockIdx.x, dim=1 to blockIdx.y, and dim=2 to blockIdx.z.
        """
        iter_var = self.frames[dim].iter_var
        return int(iter_var.dom.extent)

    def get_thread_extent(self, dim: int) -> int:
        """
        Returns the thread extent for the given dimension.
        dim=0 corresponds to threadIdx.x, dim=1 to threadIdx.y, and dim=2 to threadIdx.z.
        """
        iter_var = self.frames[-4 + dim].iter_var
        return int(iter_var.dom.extent)

    def get_num_threads(self) -> int:
        """
        Returns the thread indices from the topmost frame.
        """
        num_threads: int = 1
        for thread_dim in range(3):
            num_threads *= self.get_thread_extent(thread_dim)
        return num_threads

    @property
    def blocks(self) -> List[Var]:
        """
        Returns the block indices from the topmost frame.
        """
        return [frame.iter_var.var for frame in self.frames[0:-4]]

    @property
    def threads(self) -> List[Var]:
        """
        Returns the thread indices from the topmost frame.
        """
        return [frame.iter_var.var for frame in self.frames[-4:]]

    @property
    def num_threads(self) -> int:
        """
        Returns the total number of threads.
        """
        return self.get_num_threads()

def Kernel(
    *blocks: List[tir.PrimExpr],
    threads: Union[int, List[int], Tuple] = 128,
    prelude: Optional[str] = None,
):
    """Tools to quickly construct a GPU kernel launch frame.

    Parameters
    ----------
    blocks : List[int]
        A list of extent, can be 1-3 dimension, representing gridDim.(x|y|z)
    threads : int
        A integer representing blockDim.x
        Or a list of integers representing blockDim.(x|y|z)
        if the value is -1, we skip the threadIdx.x binding.
    prelude : str
        The import c code of the kernel,
        will be injected before the generated kernel code.
    layout_annotation: Optional[Map[tir.Buffer, tir.IndexMap]]
        The layout annotation map, used to annotate the layout of the buffers.

    Returns
    -------
    res : Tuple[frame.LaunchThreadFrame]
        The result LaunchThreadFrame.
    """
    attrs: dict = {}

    if isinstance(threads, int):
        threads = [threads, 1, 1]
    elif isinstance(threads, list):
        threads = threads + [1] * (3 - len(threads))
    elif isinstance(threads, tuple):
        threads = list(threads) + [1] * (3 - len(threads))
    else:
        raise ValueError("threads must be an integer or a list of integers")

    if prelude is not None:
        attrs["pragma_import_c"] = prelude

    return _ffi_api.KernelLaunch(blocks, threads, attrs)