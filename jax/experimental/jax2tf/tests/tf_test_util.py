# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import logging
import numpy as np
from typing import Any, Callable, List, Optional, Sequence
import tensorflow as tf  # type: ignore[import]

import jax
from jax.config import config
from jax import dtypes
from jax.experimental import jax2tf
from jax.interpreters import masking
from jax import test_util as jtu
from jax import tree_util
from jax import numpy as jnp


def _make_tf_args(args):

  def _convert_if_bfloat16(v):
    if hasattr(v, "dtype"):
      return tf.convert_to_tensor(
          np.array(v, jnp.float32) if v.dtype == jnp.bfloat16 else v,
          jax2tf.jax2tf.to_tf_dtype(v.dtype))
    return v

  return tf.nest.map_structure(_convert_if_bfloat16, args)


def _make_tf_input_signature(*tf_args) -> List[tf.TensorSpec]:
  # tf_args can be PyTrees
  def _make_one_arg_signature(tf_arg):
    return tf.TensorSpec(np.shape(tf_arg), tf_arg.dtype)

  return tf.nest.map_structure(_make_one_arg_signature, list(tf_args))


def _run_tf_function(func_tf: Callable, *tf_args, mode: str):
  if mode == "eager":
    return func_tf(*tf_args)  # EAGER
  elif mode == "graph":
    return tf.function(
        func_tf,
        autograph=False,
        input_signature=_make_tf_input_signature(*tf_args))(*tf_args)  # GRAPH
  elif mode == "compiled":
    # Adding an explicit input_signature prevents TF from constant-folding
    # the computation eagerly before compilation
    return tf.function(
        func_tf,
        autograph=False,
        experimental_compile=True,
        input_signature=_make_tf_input_signature(*tf_args))(
            *tf_args)  # COMPILED
  else:
    assert False, (
        f"Expected 'eager', 'graph', or 'compiled' for mode: got '{mode}'")


class JaxToTfTestCase(jtu.JaxTestCase):

  def setUp(self):
    super().setUp()
    # Ensure that all TF ops are created on the proper device (TPU or GPU or CPU)
    # TODO(necula): why doesn't TF do this automatically?
    tf_preferred_devices = (
        tf.config.list_logical_devices("TPU") +
        tf.config.list_logical_devices("GPU") +
        tf.config.list_logical_devices())
    self.tf_default_device = tf_preferred_devices[0]
    logging.info(f"Running jax2tf converted code on {self.tf_default_device}.")
    if jtu.device_under_test() != "gpu":
      # TODO(necula): Change the build flags to ensure the GPU is seen by TF
      # It seems that we need --config=cuda build flag for this to work?
      self.assertEqual(jtu.device_under_test().upper(),
                       self.tf_default_device.device_type)

    with contextlib.ExitStack() as stack:
      stack.enter_context(tf.device(self.tf_default_device))
      self.addCleanup(stack.pop_all().close)

  def assertDtypesMatch(self, x, y, *, canonicalize_dtypes=True):
    """Compares dtypes across JAX and TF dtypes. Overrides super method."""

    def to_numpy_dtype(dt):
      return dt if isinstance(dt, np.dtype) else dt.as_numpy_dtype

    if not config.FLAGS.jax_enable_x64 and canonicalize_dtypes:
      self.assertEqual(
          dtypes.canonicalize_dtype(to_numpy_dtype(jtu._dtype(x))),
          dtypes.canonicalize_dtype(to_numpy_dtype(jtu._dtype(y))))
    else:
      self.assertEqual(
          to_numpy_dtype(jtu._dtype(x)), to_numpy_dtype(jtu._dtype(y)))

  def ConvertAndCompare(self,
                        func_jax: Callable,
                        *args,
                        enable_xla: bool = True,
                        limitations: Optional[Callable[[str, str],
                                                       Sequence]] = None,
                        custom_assert: Optional[Callable] = None,
                        always_custom_assert: bool = False,
                        atol=None,
                        rtol=None):
    """Compares jax_func(*args) with convert(jax_func)(*args).

    It compares the result of JAX, TF ("eager" mode),
    TF with tf.function ("graph" mode), and TF with
    tf.function(experimental_compile=True) ("compiled" mode). In each mode,
    either we expect to encounter a known limitation, or the value should
    match the value from the JAX execution.

    Args:
      func_jax: the function to invoke (``func_jax(*args)``)
      args: the arguments.
      enable_xla: if True, allows the use of XLA ops in jax2tf.convert
        (default: True).
      limitations: a function that will be invoked with the device_under_test,
        e.g., "tpu", and the current TF compilation mode, e.g., "eager", to get
        the set of applicable limitations for this harness. If non-empty then
        expect a failure.
      custom_assert: a function that will be called `custom_assert(result_jax,
        result_tf)` to assert equality of the results. The result_tf has been
        converted to numpy. Use this function when
        JAX and TF produce different results. This function is only used for
        "eager" and "graph" modes by default, not for the "compiled" mode,
        because in that case we expect the results to
        be equal (default: None).
      always_custom_assert: if True, custom_assert is also called in "compiled"
        mode. This is useful in cases where JAX and TF produce different but
        equally valid results (default: False).
    """
    # Run JAX. Should not fail, we assume that the harness has been filtered
    # already by JAX unimplemented primitives.
    result_jax = func_jax(*args)  # JAX
    result_tf = None

    func_tf = jax2tf.convert(func_jax, enable_xla=enable_xla)
    tf_args = _make_tf_args(args)

    unexpected_successes = []
    for mode in ("compiled", "eager", "graph"):
      try:
        result_tf = _run_tf_function(func_tf, *tf_args, mode=mode)
        tf_exception = None
      except Exception as e:
        tf_exception = e

      if limitations is not None:
        dut = jtu.device_under_test()
        lookup_mode = mode if dut != "tpu" else "compiled"
        jax2tf_limits = limitations(dut, lookup_mode)
      else:
        jax2tf_limits = []

      expect_tf_error = any([l.expect_tf_error for l in jax2tf_limits])
      if tf_exception:
        if expect_tf_error:
          logging.info(
              f"[{self._testMethodName}] Found expected TF {mode} failure "
              f" {tf_exception}; enabled limitations {[l.description for l in jax2tf_limits if l.expect_tf_error]}"
          )
          continue
        else:
          raise tf_exception
      else:
        if expect_tf_error:
          # It is more ergonomic to print all successful modes once
          msg = (f"mode: {mode}; enabled limitations "
                 f"{[l.description for l in jax2tf_limits if l.expect_tf_error]}")
          unexpected_successes.append(msg)
          logging.warning(f"Unexpected successful mode: {msg}")
        else:
          pass

      # Convert results to np.arrays
      result_tf = tf.nest.map_structure(lambda t: t.numpy(), result_tf)  # type: ignore

      def max_with_None(tol1, tol2):
        if tol1 is None:
          return tol2
        elif tol2 is None:
          return tol1
        else:
          return max(tol1, tol2)
      max_tol = None
      max_tol = max_with_None(max_tol, atol)
      max_tol = max_with_None(max_tol, rtol)
      had_custom_assert = False
      if max_tol is not None:
        logging.info(
          f"[{self._testMethodName}] mode={mode} Starting with tol={max_tol}")
      disable_comparison = any([l.disable_comparison for l in jax2tf_limits])
      if not disable_comparison:
        for lim in jax2tf_limits:
          if lim.tol is not None:
            max_tol = max_with_None(max_tol, lim.tol)
            logging.info(
              f"[{self._testMethodName}] mode={mode}: Updating tolerance to tol={max_tol} due to {lim.description}")
          if lim.custom_assert is not None:
            logging.info(
              f"[{self._testMethodName}] mode={mode}: Running custom_assert with tol={max_tol} due to {lim.description}")
            lim.custom_assert(self, result_jax, result_tf, args=args, tol=max_tol)
            had_custom_assert = True

        if not had_custom_assert:
          if custom_assert is not None and (mode in ("eager", "graph") or
                                            always_custom_assert):
            logging.info(
              f"[{self._testMethodName}] mode={mode}: Running custom_assert with tol={max_tol} (passed as input)")
            # If we have a custom assert, use it even if we expect incorrect results
            custom_assert(self, result_jax, result_tf, args=args, tol=max_tol)
          else:
            logging.info(
              f"[{self._testMethodName}] mode={mode}: Running default assert with tol={max_tol}")
            # In compiled mode we expect the same result as JAX by default
            self.assertAllClose(result_jax, result_tf, atol=max_tol, rtol=max_tol)
      else:
        logging.warning(
          f"[{self._testMethodName}] mode={mode}: Disable numeric comparison"
          f"; enabled limitations {[l.description for l in jax2tf_limits if l.expect_tf_error]}"
        )

    if unexpected_successes:
      msg = (f"[{self._testMethodName}] The following are unexpected "
             "successful modes:\n" + "\n".join(unexpected_successes))
      logging.warning(msg)
      # Uncomment the below if you want to see warnings as failures
      #self.assertEmpty(msg)
    return result_jax, result_tf

  def TransformConvertAndCompare(self, func: Callable, arg,
                                 transform: Optional[str]):
    """Like ConvertAndCompare but first applies a transformation.

    `func` must be a function from one argument to one result. `arg` is
    the argument before the transformation.

    `transform` can be None, "jit", "jvp", "grad", "vmap", "jvp_vmap",
    "grad_vmap"
    """
    if transform is None:
      return self.ConvertAndCompare(func, arg)
    if transform == "jit":
      return self.ConvertAndCompare(jax.jit(func), arg)
    if transform == "jvp":
      t_func = lambda x, xt: jax.jvp(func, (x,), (xt,))
      return self.ConvertAndCompare(t_func, arg, np.full_like(arg, 0.1))
    if transform == "grad":
      return self.ConvertAndCompare(jax.grad(func), arg)
    if transform == "vmap":
      t_arg = np.stack([arg] * 4)
      return self.ConvertAndCompare(jax.vmap(func), t_arg)
    if transform == "jvp_vmap":
      jvp_func = lambda x, xt: jax.jvp(jax.vmap(func), (x,), (xt,))
      t_arg = np.stack([arg] * 4)
      return self.ConvertAndCompare(jvp_func, t_arg, np.full_like(t_arg, 0.1))
    if transform == "grad_vmap":
      grad_func = jax.grad(lambda x: jnp.sum(jax.vmap(func)(x)))
      t_arg = np.stack([arg] * 4)
      return self.ConvertAndCompare(grad_func, t_arg)
    assert False, transform

  def CheckShapePolymorphism(self, f_jax: Callable, *,
                             input_signature: Sequence[tf.TensorSpec],
                             in_shapes: Optional[Sequence[Any]],
                             expected_output_signature: tf.TensorSpec):
    """Convert a function using polymorphic shapes.

    Args:
      f_jax: a JAX function of `n` arguments
      input_signature: used as the input signature for the tf.function.
      in_shapes: if given, it must be a sequence of `n` shape specifications and
        must match the `input_signature`. (see jax2tf.convert).
    """
    f_tf = tf.function(
        jax2tf.convert(f_jax, in_shapes=in_shapes),
        autograph=False,
        input_signature=input_signature)
    concrete_f_tf = f_tf.get_concrete_function(*input_signature)
    if expected_output_signature:
      concrete_output_tf_shape = concrete_f_tf.output_shapes
      assert not isinstance(concrete_output_tf_shape, tuple)  # A single result
      self.assertEqual(
          tuple(expected_output_signature.shape),
          tuple(concrete_output_tf_shape))
    return f_tf

  def MakeInputSignature(self, *in_shapes):
    """From a pytree of in_shape string specification, make a pytree of tf.TensorSpec.

    Dimension variables are replaced with None.
    """

    def in_shape_to_tensorspec(in_shape: str) -> tf.TensorSpec:
      in_spec = masking.parse_spec(in_shape)
      return tf.TensorSpec(
          tuple(
              int(dim_spec) if dim_spec.is_constant else None
              for dim_spec in in_spec),
          dtype=tf.float32)

    return tree_util.tree_multimap(in_shape_to_tensorspec, in_shapes)
