import logging
from typing import Tuple, Any, List, Dict

import torch
from torch.fx.node import map_aggregate

from .quantization_state import (
    AutoQuantizationState,
)
from .utils import (
    trace_with_inputs,
    is_leaf,
    pack_weights_for_functionals,
    HookType,
    get_torch_function_hook_type,
    get_module_hook_type,
)
from . import auto_trace_rewriter

logger = logging.getLogger('auto_trace')
logging.basicConfig(level=logging.DEBUG)
# logging.basicConfig(level=logging.INFO)

# enabling this tanks performance, make sure to disable for benchmarking
# TODO(future PR): clean this up
enable_logging = False
# enable_logging = True


def add_auto_observation(
    model : torch.nn.Module,
    example_inputs: Tuple[Any],
    input_dtypes: Any = (torch.float,),  # must be same structure as model inputs
    output_dtypes: Any = (torch.float,),  # must be same structure as model outputs
) -> torch.nn.Module:
    def convert_to_interception_proxy(x):
        if isinstance(x, torch.Tensor):
            return x.as_subclass(QuantizationInterceptionProxy)  # type: ignore[arg-type]
        else:
            return x

    cur_module = None
    first_call = True
    module_stack : List[torch.nn.Module] = []
    # Counter for tensor IDs, will be modified inplace by quant state.
    # This is used to track tensors from output ops to input ops. For example,
    # if op_n had a tensor output with id=1, and op_n+2 had a tensor input with
    # id=1, we know that the output of op_n is the input to op_n+2.
    qtensor_id = [0]
    module_id_to_fqn: Dict[int, str] = {}

    class QuantizationInterceptionProxy(torch.Tensor):
        """
        An override of `torch.Tensor` to enable dynamic tracing for
        quantization.

        For each function with a `__torch_function__` override, this proxy does
        the following for functions which need quantization:

        1. calls `_auto_quant_state.validate_cur_op` to validate that
           the currently seen op is the same as what was recorded during tracing
        2. calls `_auto_quant_state.op_prepare_before_hook`
        3. executes the original function
        4. calls `_auto_quant_state.op_prepare_after_hook`
        5. calls `_auto_quant_state.mark_cur_op_complete` to increment
           the current op index in preparation for the next op

        Otherwise, calls the original function.
        """

        @classmethod
        def __torch_function__(cls, func, types, args=(), kwargs=None):
            # to prevent printing things from going into an infinite loop
            if func == torch.Tensor.__repr__:
                return super().__torch_function__(func, types, args, kwargs)
            if enable_logging:
                logger.debug(f'__torch_function__ {str(func)} len_args {len(args)}')

            nonlocal qtensor_id
            nonlocal cur_module
            kwargs = kwargs if kwargs else {}
            # if we are in a function, the current module is always a parent
            parent_module = cur_module
            hook_type = get_torch_function_hook_type(parent_module, func)

            if hook_type is HookType.OP_HOOKS:
                assert parent_module is not None
                qstate = parent_module._auto_quant_state
                fqn = module_id_to_fqn[id(parent_module)] if parent_module else None
                if not first_call:
                    qstate.validate_cur_op(func)
                # run "before" hook
                args, kwargs = qstate.op_prepare_before_hook(
                    func, args, kwargs, first_call, qtensor_id, fqn, parent_module)
                # forward
                output = super().__torch_function__(func, types, args, kwargs)
                # run "after" hook
                output = qstate.op_prepare_after_hook(
                    func, output, args, first_call, qtensor_id, parent_module)
                qstate.mark_cur_op_complete(func)
            else:
                output = super().__torch_function__(func, types, args, kwargs)

            # TODO: is this right? Don't really understand this
            if output is NotImplemented:
                with torch._C.DisableTorchFunction():
                    output = func(*args, **kwargs).as_subclass(
                        QuantizationInterceptionProxy)
                assert output is not NotImplemented

            return output

        def __repr__(self):
            return f'QuantizationInterceptionProxy({super().__repr__()})'

        # TODO(future PR): add other math overrides

    class QuantizationInterceptionModule(type(model)):  # type: ignore[misc]
        """
        An override of user defined subclass of `nn.Module` to enable
        dynamic tracing for quantization.

        `cur_module` keeps track of the current module in the stack.

        During the fist call, an `AutoQuantizationState` object is created and
        attached to each non-leaf modules which we need to check for
        quantizeable operations.

        We override the `__call__` function to do the following for each
        module:

        If the module is an op which needs quantization:

        1. calls `_auto_quant_state.validate_cur_op` to validate that
           the currently seen op is the same as what was recorded during tracing
        2. calls parent module's `._auto_quant_state.op_prepare_before_hook`
        3. executes the original module forward
        4. calls parent module's `_auto_quant_state.op_prepare_after_hook`
        5. calls `_auto_quant_state.mark_cur_op_complete` to increment
           the current op index in preparation for the next op

        If the module can contain children ops that need quantization:

        1. calls `_auto_quant_state.inputs_prepare_hook` (not implemented yet)
        2. executes the original module forward
        3. calls `_auto_quant_state.outputs_prepare_hook`

        Otherwise, calls the original module forward.
        """

        __interception_module__ = True

        def __call__(self, *args, **kwargs):
            new_args = map_aggregate(args, convert_to_interception_proxy)
            new_kwargs = map_aggregate(kwargs, convert_to_interception_proxy)
            orig_module_call = torch.nn.Module.__call__
            orig_nn_sequential_forward = torch.nn.Sequential.forward

            def record_module(self, *args, **kwargs):

                if enable_logging:
                    logger.debug(f"record_module: {type(self)}")

                nonlocal cur_module
                old_module = cur_module
                cur_module = self
                try:
                    parent_module = module_stack[-1] if len(module_stack) else None
                    module_stack.append(self)
                    fqn = module_id_to_fqn.get(id(self), None)

                    if enable_logging:
                        fqn = module_id_to_fqn.get(id(self), None)
                        logger.debug(f"\nstarting fqn {fqn}")

                    hook_type = get_module_hook_type(parent_module, cur_module)

                    if hook_type is HookType.OP_HOOKS:
                        assert parent_module is not None
                        parent_qstate = parent_module._auto_quant_state
                        assert isinstance(parent_qstate, AutoQuantizationState)
                        # before hooks
                        if not first_call:
                            parent_qstate.validate_cur_op(cur_module)
                        args, kwargs = parent_qstate.op_prepare_before_hook(
                            cur_module, args, kwargs, first_call, qtensor_id,
                            fqn, cur_module)

                        # original forward
                        output = orig_module_call(self, *args, **kwargs)

                        # after hooks
                        # TODO is it correct to call_cur_module twice here?
                        output = parent_qstate.op_prepare_after_hook(
                            cur_module, output, args, first_call, qtensor_id,
                            cur_module)
                        parent_qstate.mark_cur_op_complete(cur_module)

                    elif hook_type is HookType.MODULE_IO_HOOKS:
                        # TODO(future PR): add inputs io hook

                        cur_qstate = cur_module._auto_quant_state
                        cur_qstate.validate_is_at_first_idx()

                        # original forward
                        output = orig_module_call(self, *args, **kwargs)

                        # after hooks
                        assert isinstance(cur_qstate, AutoQuantizationState)
                        output = cur_qstate.outputs_prepare_hook(
                            output, first_call, qtensor_id)
                        cur_qstate.validate_is_at_last_seen_idx()

                    elif hook_type is HookType.ARG_DEQUANTS:
                        output = orig_module_call(self, *args, **kwargs)
                        # if this fp32 was inplace, make sure to set the output dtype
                        # back to torch.float
                        if hasattr(output, '_qtensor_info'):
                            del output._qtensor_info

                    else:
                        output = orig_module_call(self, *args, **kwargs)

                    if enable_logging:
                        fqn = module_id_to_fqn.get(id(self), None)
                        logger.debug(f"\nending fqn {fqn}")

                    return output
                finally:
                    module_stack.pop()
                    cur_module = old_module

            torch.nn.Module.__call__ = record_module
            torch.nn.Sequential.forward = _nn_sequential_patched_forward  # type: ignore[assignment]
            nonlocal first_call
            try:
                # Create a list before iterating because we are adding new
                # named modules inside the loop.
                named_modules = list(self.named_modules())
                for k, v in named_modules:

                    # k is the global FQN, i.e. 'foo.bar.baz'
                    # v is the module instance
                    #
                    # we need to associate the global FQN with SeenOp
                    # for modules, this is the module FQN
                    # for functions, this is the parent module FQN
                    module_id_to_fqn[id(v)] = k

                    has_qconfig = hasattr(v, 'qconfig') and v.qconfig is not None
                    if has_qconfig and not is_leaf(v):
                        if first_call:
                            if v is self:
                                # for the top level module only, specify input
                                # and output dtypes
                                v._auto_quant_state = AutoQuantizationState(
                                    v.qconfig, input_dtypes, output_dtypes)
                                pass
                            else:
                                v._auto_quant_state = AutoQuantizationState(
                                    v.qconfig)
                        else:
                            if not isinstance(v, AutoQuantizationState):
                                assert hasattr(v, '_auto_quant_state')
                                v._auto_quant_state.reset_to_new_call()

                output = super().__call__(*new_args, **new_kwargs)
                return output
            finally:
                torch.nn.Module.__call__ = orig_module_call
                torch.nn.Sequential.forward = orig_nn_sequential_forward  # type: ignore[assignment]
                first_call = False


    model.__class__ = QuantizationInterceptionModule
    # create the graph
    trace_with_inputs(model, example_inputs)
    return model


# TODO(future PR): add serialization support
def add_auto_convert(module : torch.nn.Module) -> torch.nn.Module:
    def convert_to_dispatch_proxy(x):
        if isinstance(x, torch.Tensor):
            return x.as_subclass(QuantizationDispatchProxy)  # type: ignore[arg-type]
        else:
            return x

    module_id_to_fqn: Dict[int, str] = {}

    class QuantizationDispatchProxy(torch.Tensor):
        """
        An override of `torch.Tensor` to enable dynamic dispatch for
        quantization inference.

        For each function with a `__torch_fuction__` override, this proxy does
        the following for functions which need quantization:

        1. calls `_auto_quant_state.validate_cur_op` to validate that
           the currently seen op is the same as what was recorded during tracing
        2. calls `_auto_quant_state.op_convert_before_hook`.
        3. executes the function, with target, args and kwargs possibly modified
           by (2)
        4. calls `_auto_quant_state.inference_function_after_hook`.
        5. calls `_auto_quant_state.mark_cur_op_complete` to increment
           the current op index in preparation for the next op

        Otherwise, calls the original function.
        """

        @classmethod
        def __torch_function__(cls, func, types, args=(), kwargs=None):
            # to prevent printing things from going into an infinite loop
            if func == torch.Tensor.__repr__:
                return super().__torch_function__(func, types, args, kwargs)

            kwargs = kwargs if kwargs else {}
            # if we are in a function, the current module is always a parent
            parent_module = cur_module
            hook_type = get_torch_function_hook_type(parent_module, func)

            if enable_logging:
                with torch._C.DisableTorchFunction():
                    logger.debug(
                        f"__torch_function__ {func} " +
                        f"hook_type {hook_type} " +
                        # f"arg_types {[type(arg) for arg in args]}) " +
                        f"arg_dtypes {[arg.dtype if isinstance(arg, torch.Tensor) else None for arg in args]}")

            if hook_type is HookType.OP_HOOKS:
                assert parent_module is not None
                qstate = parent_module._auto_quant_state
                # before hooks
                qstate.validate_cur_op(func)
                func, args, kwargs = qstate.op_convert_before_hook(
                    func, args, kwargs, parent_module)

                # forward
                output = super().__torch_function__(func, types, args, kwargs)
                # after hooks
                output = qstate.op_convert_after_hook(func, output)
                qstate.mark_cur_op_complete(func)

            elif hook_type is HookType.ARG_DEQUANTS:
                # disabling torch function to prevent infinite recursion on
                # getset
                # TODO(future PR): handle more dtypes
                with torch._C.DisableTorchFunction():
                    new_args = []
                    for arg in args:
                        if isinstance(arg, torch.Tensor) and arg.is_quantized:
                            new_args.append(arg.dequantize())
                        else:
                            new_args.append(arg)
                    args = tuple(new_args)
                output = super().__torch_function__(func, types, args, kwargs)

            else:  # HookType.NONE
                output = super().__torch_function__(func, types, args, kwargs)

            # TODO: is this right? Don't really understand this
            if output is NotImplemented:
                with torch._C.DisableTorchFunction():
                    output = func(*args, **kwargs).as_subclass(
                        QuantizationDispatchProxy)
                assert output is not NotImplemented

            if enable_logging:
                out_dtype = None
                if isinstance(output, torch.Tensor):
                    out_dtype = output.dtype
                logger.debug(f"__torch_function__ {func} out {out_dtype} end")

            return output

        def __repr__(self):
            return f'QuantizationDispatchProxy({super().__repr__()})'

    cur_module = None
    module_stack : List[torch.nn.Module] = []

    assert len(module.__class__.__bases__) == 1

    class QuantizationDispatchModule(module.__class__.__bases__[0]):  # type: ignore[name-defined]
        """
        An override of user defined subclass of `nn.Module` to enable
        dynamic tracing for quantization, after model conversion
        to quantized domain.

        `cur_module` keeps track of the current module in the stack.

        Tensor arguments are converted to `QuantizationDispatchProxy`.

        We override the `__call__` function to do the following for each
        module:

        If the module is an op which needs quantization:

        1. calls `_auto_quant_state.validate_cur_op` to validate that
           the currently seen op is the same as what was recorded during tracing
        2. calls parent module's `._auto_quant_state.op_convert_before_hook`
        3. executes the original module forward
        4. calls parent module's `_auto_quant_state.op_convert_after_hook`
        5. calls `_auto_quant_state.mark_cur_op_complete` to increment
           the current op index in preparation for the next op

        If the module can contain children ops that need quantization:

        1. calls `_auto_quant_state.inputs_convert_hook` (not implemented yet)
        2. executes the original module forward
        3. calls `_auto_quant_state.outputs_convert_hook`

        Otherwise, calls the original module forward.
        """

        def __call__(self, *args, **kwargs):
            new_args = map_aggregate(args, convert_to_dispatch_proxy)
            new_kwargs = map_aggregate(kwargs, convert_to_dispatch_proxy)
            orig_module_call = torch.nn.Module.__call__
            orig_nn_sequential_forward = torch.nn.Sequential.forward

            def record_module(self, *args, **kwargs):
                if enable_logging:
                    fqn = module_id_to_fqn.get(id(self), None)
                    logger.debug(f"\nstarting fqn {fqn}")

                nonlocal cur_module
                old_module = cur_module
                cur_module = self
                try:
                    parent_module = module_stack[-1] if len(module_stack) else None
                    module_stack.append(self)
                    hook_type = get_module_hook_type(parent_module, cur_module)
                    if enable_logging:
                        logger.debug(
                            f"record_module {type(self)} " +
                            # f"arg_types {[type(arg) for arg in args]} " +
                            f"arg_dtypes {[arg.dtype if isinstance(arg, torch.Tensor) else None for arg in args]} " +
                            f"hook_type {hook_type}")

                    if hook_type is HookType.OP_HOOKS:
                        # before hooks
                        assert parent_module is not None
                        assert isinstance(parent_module._auto_quant_state, AutoQuantizationState)
                        qstate = parent_module._auto_quant_state
                        if enable_logging:
                            logger.debug(qstate)
                        qstate.validate_cur_op(cur_module)
                        _, args, kwargs = qstate.op_convert_before_hook(
                            cur_module, args, kwargs, cur_module)
                        # forward
                        output = orig_module_call(self, *args, **kwargs)
                        # after hooks
                        output = qstate.op_convert_after_hook(cur_module, output)
                        qstate.mark_cur_op_complete(cur_module)

                    elif hook_type is HookType.MODULE_IO_HOOKS:
                        cur_qstate = cur_module._auto_quant_state
                        if enable_logging:
                            logger.debug(cur_qstate)

                        cur_qstate.validate_is_at_first_idx()

                        # before hooks (TODO)
                        # forward
                        output = orig_module_call(self, *args, **kwargs)
                        # after hooks
                        assert isinstance(cur_qstate, AutoQuantizationState)
                        output = cur_qstate.outputs_convert_hook(output)
                        cur_qstate.validate_is_at_last_seen_idx()

                    elif hook_type is HookType.ARG_DEQUANTS:
                        # disabling torch function to prevent infinite recursion on
                        # getset
                        # TODO(future PR): handle more dtypes
                        with torch._C.DisableTorchFunction():
                            new_args = []
                            for arg in args:
                                if isinstance(arg, torch.Tensor) and arg.is_quantized:
                                    dequant = arg.dequantize().as_subclass(
                                        QuantizationDispatchProxy)  # type: ignore[arg-type]
                                    new_args.append(dequant)
                                else:
                                    new_args.append(arg)
                            args = tuple(new_args)
                        output = orig_module_call(self, *args, **kwargs)

                    else:
                        output = orig_module_call(self, *args, **kwargs)

                    if enable_logging:
                        logger.debug(
                            f"record_module {type(self)} " +
                            # f"out {type(output)} " +
                            f"dtype {output.dtype if isinstance(output, torch.Tensor) else None} " +
                            "end")
                        logger.debug(f"ending fqn {fqn}\n")
                    return output
                finally:
                    module_stack.pop()
                    cur_module = old_module

            torch.nn.Module.__call__ = record_module
            torch.nn.Sequential.forward = _nn_sequential_patched_forward  # type: ignore[assignment]

            try:
                for k, v in self.named_modules():
                    module_id_to_fqn[id(v)] = k
                    if hasattr(v, '_auto_quant_state'):
                        v._auto_quant_state.reset_to_new_call()

                needs_io_hooks = hasattr(self, '_auto_quant_state')

                # handle module input dtype conversions
                # TODO(implement)

                output = super().__call__(*new_args, **new_kwargs)

                # handle module output dtype conversions
                if needs_io_hooks:
                    qstate = self._auto_quant_state
                    assert isinstance(qstate, AutoQuantizationState)
                    output = qstate.outputs_convert_hook(output)

                def unwrap_proxy(a):
                    if isinstance(a, QuantizationDispatchProxy):
                        a.__class__ = torch.Tensor  # type: ignore[assignment]
                    return a
                output = map_aggregate(output, unwrap_proxy)
                return output
            finally:
                torch.nn.Module.__call__ = orig_module_call
                torch.nn.Sequential.forward = orig_nn_sequential_forward  # type: ignore[assignment]

        def rewrite_for_scripting(self):
            return auto_trace_rewriter.rewrite_for_scripting(self)

    pack_weights_for_functionals(module)
    module.__class__ = QuantizationDispatchModule

    return module


# AutoQuantizationState lives in parent module's _modules.
# Currently, `torch.nn.Sequential`'s forward iterates over all
# items in _modules. To avoid changing the meaning of the program, for
# now we patch the forward to ignore our quantization state.
# Note: this is a hackedy hack, before launching we should consider
# checking the fix into `torch.nn.Sequential` to avoid the patch.
def _nn_sequential_patched_forward(cls, input):
    for module in cls:
        if not isinstance(module, AutoQuantizationState):
            input = module(input)
    return input
