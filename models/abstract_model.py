# coding=utf-8
# Copyright 2019 The Tensor2Robot Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""TFModel abstract subclasses."""

from __future__ import absolute_import
from __future__ import division

from __future__ import print_function

import abc
import os
from absl import flags
from absl import logging
import gin
from gin.tf import utils as gin_utils
import numpy as np

from tensor2robot.models import model_interface
from tensor2robot.models import optimizers
from tensor2robot.preprocessors import abstract_preprocessor
from tensor2robot.preprocessors import noop_preprocessor
from tensor2robot.utils import tensorspec_utils
import tensorflow as tf

from typing import Any, Callable, Dict, Optional, Text, Tuple, Union

FLAGS = flags.FLAGS
TRAIN = tf.estimator.ModeKeys.TRAIN
EVAL = tf.estimator.ModeKeys.EVAL
PREDICT = tf.estimator.ModeKeys.PREDICT

RunConfigType = Optional[
    Union[tf.estimator.RunConfig, tf.contrib.tpu.RunConfig]]
ParamsType = Optional[Dict[Text, Any]]

DictOrSpec = Union[Dict[Text, tf.Tensor], tensorspec_utils.TensorSpecStruct]

ModelTrainOutputType = Union[tf.Tensor, Tuple[tf.Tensor, DictOrSpec]]
ExportOutputType = Union[Dict[Text, tf.Tensor], Tuple[
    Dict[Text, tf.Tensor], Dict[Text, tf.estimator.export.PredictOutput]]]

try:
  flags.DEFINE_string('master', '', 'Master for TPU RunConfig')
except flags.DuplicateFlagError:
  pass

DEVICE_TYPE_CPU = 'cpu'
DEVICE_TYPE_GPU = 'gpu'
DEVICE_TYPE_TPU = 'tpu'

gin_configurable_run_config_cls = gin.external_configurable(
    tf.estimator.RunConfig,
    name='tf.estimator.RunConfig',
    blacklist=['model_dir'])

gin_configurable_tpu_run_config_cls = gin.external_configurable(
    tf.contrib.tpu.RunConfig,
    name='tf.contrib.tpu.RunConfig',
    blacklist=['model_dir', 'tpu_config'])

gin_configurable_tpu_config_cls = gin.external_configurable(
    tf.contrib.tpu.TPUConfig, name='tf.contrib.tpu.TPUConfig')


@gin.configurable
def default_init_from_checkpoint_fn(checkpoint):
  """init_from_checkpoint_fn that can be used to init a model from a checkpoint.

  Args:
    checkpoint: String pointing to path of TF checkpoint.
  """
  logging.info('Initializing model weights from %s', checkpoint)
  variables_to_restore = tf.contrib.framework.get_trainable_variables()
  assignment_map = {v.op.name: v for v in variables_to_restore}
  tf.train.init_from_checkpoint(checkpoint, assignment_map)


@gin.configurable
class AbstractT2RModel(model_interface.ModelInterface):
  """Base class encapsulating a model_fn and metadata about input/output sizes.

  The `T2RModel` abstraction defines a `model_fn` method that can be constructed
  using inputs coming from a tf.data.Dataset, or placeholders. We generate
  these automatically for tf.Example using an input_generator and preprocessor
  if no custom version is provided. Note, in order to work with task_explore
  episode.proto and state.proto, a custom input_generator has to be provided.
  The model_fn should not do any preprocessing. If any preprocessing besides
  the raw input tensors is necessary please use a custom preprocessor.

  """
  __metaclass__ = abc.ABCMeta

  def __init__(self,
               preprocessor_cls=None,
               create_optimizer_fn=optimizers.default_create_optimizer_fn,
               device_type = DEVICE_TYPE_CPU,
               summarize_gradients = True,
               use_sync_replicas_optimizer = False,
               use_avg_model_params = False,
               init_from_checkpoint_fn=None):
    """Base constructor to be used by subclass.

    Args:
      preprocessor_cls: (Optional) A class derived from
        preprocessors.AbstractPreprocessor.
      create_optimizer_fn: A callable function which returns an instance of a
        subclass of tf.train.Optimizer. We couldn't take an optimizer instance
        here because some optimizer's constructor may need to access the
        variables in the graph, which will be created by Estimator when calling
        model_fn. More precisely we will only create an instance during training
        (mode == ModeKeys.TRAIN) within the _optimizer property call which will
        wrap the optimizer instance for GPU towers or TPUs if necessary. The
        _optimizer property is only used within create_train_op function.
      device_type: The device type this model will be deployed on (
        DEVICE_TYPE_CPU, DEVICE_TYPE_GPU, DEVICE_TYPE_TPU).
      summarize_gradients: If True summaries for the gradients produced by the
        train_op will be created. Note, we will automatically disable these
        summaries in case of DEVICE_TYPE_TPU.
      use_sync_replicas_optimizer: If True, synchronize gradient updates from
        the different replicas. (GPU-only, since TPUs are already synchronous).
      use_avg_model_params: During training use a MovingAverageOptimizer and
        swapping saver to compute a running average of the model variables for
        inference.
      init_from_checkpoint_fn: A function that calls
        tf.train.init_from_checkpoint.
    """
    self._preprocessor_cls = preprocessor_cls
    self._create_optimizer_fn = create_optimizer_fn
    self._device_type = device_type
    self._summarize_gradients = summarize_gradients
    self._use_sync_replicas_optimizer = use_sync_replicas_optimizer
    self._sync_replicas_optimizer = None
    self._use_avg_model_params = use_avg_model_params
    self._init_from_checkpoint_fn = init_from_checkpoint_fn
    self._optimizer = None  # type: Optional[tf.train.Optimizer]
    self._scaffold_fn = tf.train.Scaffold

  def create_pack_features(self,
                           feature_spec,
                           label_spec
                          ):
    """Creates a callable function which packs features for model inference.

    Note, it is important that this function is very much independent of the
    model intrinsics. To achieve hermetic models using exported saved
    models this function should not depend on parameters such that previously
    exported models break or have to be aware of such parameters. Pack features
    is required to map from other data representations, e.g. python objects and
    protos to the saved model inputs. In essence, pack features allows to
    extract a feed_dict which is fed to the loaded saved model.

    Args:
      feature_spec: tensorspec_utils.TensorSpecStruct input feature
        specifications of the model.
      label_spec: tensorspec_utils.TensorSpecStruct input label specifications
        of the model.
    """
    raise NotImplementedError()

  def get_feature_specification_for_packing(
      self, mode):
    """Returns the feature_spec that create_pack_features expects."""
    return self.preprocessor.get_in_feature_specification(mode)

  def get_label_specification_for_packing(self, mode
                                         ):
    """Returns the label_spec that create_pack_features expects."""
    return self.preprocessor.get_in_label_specification(mode)

  @property
  def default_preprocessor_cls(self):
    """The default preprocessor class if the user has not provided another one.

    We use this property to define the preprocessor class in order to avoid
    dependencies between the super constructor invocation.

    Returns:
      A preprocessor class.
    """
    return noop_preprocessor.NoOpPreprocessor

  @property
  def preprocessor(self):
    feature_specification_fn = self.get_feature_specification
    label_specification_fn = self.get_label_specification

    preprocessor_cls = self._preprocessor_cls
    if preprocessor_cls is None:
      preprocessor_cls = self.default_preprocessor_cls

    return preprocessor_cls(
        model_feature_specification_fn=feature_specification_fn,
        model_label_specification_fn=label_specification_fn,
        is_model_device_tpu=self.is_device_tpu)

  @property
  def scaffold_fn(self):
    """Returns a scaffold function object for model loading."""
    return self._scaffold_fn

  #############################################################################
  # START DEPRECATED functions which will be removed soon.
  #############################################################################
  def get_eval_hooks(self, config, params):
    """Get eval_hooks to be passed to estimator spec."""
    logging.warning('This function is deprecated and will be replaced.')
    hooks = []
    summary_op = tf.summary.merge_all()
    if summary_op is not None:
      eval_name = 'eval'
      if params is not None:
        eval_name = params.get('eval_name', eval_name)
      hooks = [
          tf.train.SummarySaverHook(
              output_dir=os.path.join(config.model_dir, eval_name),
              save_steps=config.save_summary_steps,
              summary_op=summary_op)
      ]
    return hooks

  #############################################################################
  # END DEPRECATED functions which will be removed soon.
  #############################################################################

  @abc.abstractmethod
  def get_feature_specification(
      self, mode):
    """Required features for the model_fn/model_inference_fn.

    Note, the model_fn might use additional features for debugging/development
    purposes. The create_export_outputs_fn will however only require the
    specified required features. Only this subset of features will be used to
    generate automatic tf.Example extractors and numpy placeholders for the
    serving models.

    Args:
      mode: The mode for feature specifications
    """

  @abc.abstractmethod
  def get_label_specification(self, mode
                             ):
    """Required labels for the model_fn/model_train_fn/model_eval_fn.

    Note, the model_fn might use additional labels for debugging/development
    purposes.

    Args:
      mode: The mode for feature specifications
    """

  def create_train_op(self, loss, optimizer):
    """Create the train_op of from the loss obtained from model_train_fn.

    Args:
      loss: The loss we compute within model_train_fn.
      optimizer: An instance of `tf.train.Optimizer`.

    Returns:
      train_op: Op for the training step.
    """
    if self.is_device_tpu:
      # TPUs don't support summaries up until now. Hence, we overwrite the user
      # provided summarize_gradients option to False.
      if self._summarize_gradients:
        logging.info('We cannot use summarize_gradients on TPUs.')
      return tf.contrib.training.create_train_op(
          loss, optimizer, summarize_gradients=False)
    return tf.contrib.training.create_train_op(
        loss, optimizer, summarize_gradients=self._summarize_gradients)

  def maybe_init_from_checkpoint(self):
    """Optionally initialize the model from a checkpoint.

    We only automatically initialize from a checkpoint other than the model_dir
    if this function is overloaded with an actual model specific implementation.
    The recommended way to initialize a model from a checkpoint is done via
    tf.train.init_from_checkpoint(ckpt_dir_or_file, assignment_map).
    """
    if self._init_from_checkpoint_fn:
      self._init_from_checkpoint_fn()

  def raise_no_tpu_support(self):
    """Convenience function to raise on tpu request for an unsupported model.

    Raises:
      ValueError: If the model should run on a TPU.
    """
    if self.is_device_tpu:
      raise ValueError('This model {} does not support TPUs'.format(
          self.__name__))

  @abc.abstractmethod
  def inference_network_fn(self,
                           features,
                           labels,
                           mode,
                           config = None,
                           params = None):
    """The inference network implementation.

    This creates the main network based on features.
    Optionally (mode=ModeKeys.TRAIN or ModeKeys.EVAL) the model can do
    additional processing on labels, however, it has to be ensured that this is
    optional and the graph is fully operational without labels. At inference
    time we will have no access to labels. Tensors which are required for loss
    computation or debugging must be put into the inference_outputs dict.
    Having a dedicated inference_network_fn allows to compose new networks by
    using other TFModels.

    Please, use the following pattern to add not supported tpu model components
    such as tf.summary.*
    if self.use_summaries(params):
      # Do operations which are not supported on tpus.

    If your model does not support TPUs at all, please call the following
    function.
    self.raise_no_tpu_support()

    Args:
      features: This is the first item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      labels: This is the second item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      mode: (ModeKeys) Specifies if this is training, evaluation or prediction.
      config: (Optional tf.estimator.RunConfig or tf.contrib.tpu.RunConfig) Will
        receive what is passed to Estimator in config parameter, or the default
        config (tf.estimator.RunConfig). Allows updating things in your model_fn
        based on  configuration such as num_ps_replicas, or model_dir.
      params: An optional dict of hyper parameters that will be passed into
        input_fn and model_fn. Keys are names of parameters, values are basic
        python types. There are reserved keys for TPUEstimator, including
        'batch_size'.

    Returns:
      inference_outputs: A dict with output tensors.
    """

  @abc.abstractmethod
  def model_train_fn(self,
                     features,
                     labels,
                     inference_outputs,
                     mode,
                     config = None,
                     params = None):
    """The training model implementation.

    This model_fn should add the loss computation based on the inference_outputs
    and labels. For better debugging we also provide access to the input
    features. Note, no new variables should be generated in this model_fn since
    the model_inference_fn and the maybe_init_from_checkpoint function would
    not have access to these variables. We output the final loss (scalar) and
    a dict of optional train_outputs which might be useful for the
    model_eval_fn.

    Please, use the following pattern to add not supported tpu model components
    such as tf.summary.*
    if self.use_summaries(params):
      # Do operations which are not supported on tpus.

    If your model does not support TPUs at all, please call the following
    function.
    self.raise_no_tpu_support()

    Args:
      features: This is the first item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      labels: This is the second item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      inference_outputs: A dict containing the output tensors of
        model_inference_fn.
      mode: (ModeKeys) Specifies if this is training, evaluation or prediction.
      config: (Optional tf.estimator.RunConfig or tf.contrib.tpu.RunConfig) Will
        receive what is passed to Estimator in config parameter, or the default
        config (tf.estimator.RunConfig). Allows updating things in your model_fn
        based on  configuration such as num_ps_replicas, or model_dir.
      params: An optional dict of hyper parameters that will be passed into
        input_fn and model_fn. Keys are names of parameters, values are basic
        python types. There are reserved keys for TPUEstimator, including
        'batch_size'.

    Returns:
      loss: The loss we will optimize.
      train_outputs: (Optional) A dict with additional tensors the training
        model generates. We output these tensors such that model_eval_fn could
        introspect these tensors.
    """

  def model_eval_fn(self,
                    features,
                    labels,
                    inference_outputs,
                    train_loss,
                    train_outputs,
                    mode,
                    config = None,
                    params = None):
    """The eval model implementation, by default we report the loss for eval.

    This function should add the eval_metrics computation based on the
    inference_outputs, labels and the train_loss. For better debugging we also
    provide access to the input features and the train_outputs. Note, no new
    variables should be generated in this model_fn since the model_inference_fn
    and the maybe_init_from_checkpoint function would not have access to these
    variables.

    Please, use the following pattern to add not supported tpu model components
    such as tf.summary.*
    if self.use_summaries(params):
      # Do operations which are not supported on tpus.

    If your model does not support TPUs at all, please call the following
    function.
    self.raise_no_tpu_support()

    Args:
      features: This is the first item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      labels: This is the second item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      inference_outputs: A dict containing the output tensors of
        model_inference_fn.
      train_loss: The final loss from model_train_fn.
      train_outputs: A dict containing the output tensors (dict) of
        model_train_fn.
      mode: (ModeKeys) Specifies if this is training, evaluation or prediction.
      config: (Optional tf.estimator.RunConfig or tf.contrib.tpu.RunConfig) Will
        receive what is passed to Estimator in config parameter, or the default
        config (tf.estimator.RunConfig). Allows updating things in your model_fn
        based on  configuration such as num_ps_replicas, or model_dir.
      params: An optional dict of hyper parameters that will be passed into
        input_fn and model_fn. Keys are names of parameters, values are basic
        python types. There are reserved keys for TPUEstimator, including
        'batch_size'.

    Returns:
      eval_metrics: A tuple of (metric_fn, metric_fn_inputs) where metric_fn
        is a dict with {metric_description: tf.metrics.*}.
    """
    del features, labels, inference_outputs, train_loss, train_outputs
    del mode, config, params
    # By default we don't have any eval_metrics. The loss computation used
    # to optimize the model_fn will be reported for the model_eval_fn as well.
    # Hence, by default the EVAL mode can be used to determine the loss
    # performance on the eval dataset or even a larger train dataset.
    return None

  def add_summaries(self,
                    features,
                    labels,
                    inference_outputs,
                    train_loss,
                    train_outputs,
                    mode,
                    config = None,
                    params = None):
    """Add summaries to the graph.

    Having a central place to add all summaries to the graph is helpful in order
    to compose models. For example, if an inference_network_fn is used within
    a while loop no summaries can be added. This function will allow to add
    summaries after the while loop has been processed.

    Args:
      features: This is the first item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      labels: This is the second item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      inference_outputs: A dict containing the output tensors of
        model_inference_fn.
      train_loss: The final loss from model_train_fn.
      train_outputs: A dict containing the output tensors (dict) of
        model_train_fn.
      mode: (ModeKeys) Specifies if this is training, evaluation or prediction.
      config: (Optional tf.estimator.RunConfig or tf.contrib.tpu.RunConfig) Will
        receive what is passed to Estimator in config parameter, or the default
        config (tf.estimator.RunConfig). Allows updating things in your model_fn
        based on  configuration such as num_ps_replicas, or model_dir.
      params: An optional dict of hyper parameters that will be passed into
        input_fn and model_fn. Keys are names of parameters, values are basic
        python types. There are reserved keys for TPUEstimator, including
        'batch_size'.
    """
    del features, labels, inference_outputs, train_loss, train_outputs, mode
    del config
    if not self.use_summaries(params):
      return

  def create_export_outputs_fn(self,
                               features,
                               inference_outputs,
                               mode,
                               config = None,
                               params = None):
    """We export the final output used for model inference.

    This model_fn should create the optional export_outputs, see
    tf.estimator.EstimatorSpec for a more in depth description, and the
    required predictions dict. Note, the predictions dict should more often
    than not be a small subset of the inference_outputs.

    Please, use the following pattern to add not supported tpu model components
    such as tf.summary.*
    if self.use_summaries(params):
      # Do operations which are not supported on tpus.

    If your model does not support TPUs at all, please call the following
    function.
    self.raise_no_tpu_support()

    Args:
      features: This is the first item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      inference_outputs: A dict containing the output tensors of
        model_inference_fn.
      mode: (ModeKeys) Specifies if this is training, evaluation or prediction.
      config: (Optional tf.estimator.RunConfig or tf.contrib.tpu.RunConfig) Will
        receive what is passed to Estimator in config parameter, or the default
        config (tf.estimator.RunConfig). Allows updating things in your model_fn
        based on  configuration such as num_ps_replicas, or model_dir.
      params: An optional dict of hyper parameters that will be passed into
        input_fn and model_fn. Keys are names of parameters, values are basic
        python types. There are reserved keys for TPUEstimator, including
        'batch_size'.

    Returns:
      predictions: A dict of tensors.
      export_outputs: (Optional) A dict containing an arbitrary name for the
        output and tf.estimator.export.PredictOutput(output_dict) as value.
        The output dict is a {name: tensor} mapping. If None, the default
        mapping for predictions is generated. The export_outputs are used
        for the serving model. Multi-headed models should have one name
        per head.
    """
    del features, mode, config, params
    # By default we will export all outputs generated by the
    # inference_network_fn.
    return inference_outputs

  def model_fn(self,
               features,
               labels,
               mode,
               config = None,
               params = None):
    """Estimator model_fn.

    Args:
      features: This is the first item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      labels: This is the second item returned from the input_fn and parsed by
        tensorspec_utils.validate_and_pack. A spec_structure which fulfills the
        requirements of the self.get_feature_specification.
      mode: (ModeKeys) Specifies if this is training, evaluation or prediction.
      config: (Optional tf.estimator.RunConfig or tf.contrib.tpu.RunConfig) Will
        receive what is passed to Estimator in config parameter, or the default
        config (tf.estimator.RunConfig). Allows updating things in your model_fn
        based on  configuration such as num_ps_replicas, or model_dir.
      params: An optional dict of hyper parameters that will be passed into
        input_fn and model_fn. Keys are names of parameters, values are basic
        python types. There are reserved keys for TPUEstimator, including
        'batch_size'.

    Raises:
      ValueError: If the mode key is not supported, not in [PREDICT, TRAIN,
        EVAL].

    Returns:
      An EstimatorSpec.
    """

    features = tensorspec_utils.validate_and_pack(
        expected_spec=self.get_feature_specification(mode),
        actual_tensors_or_spec=features,
        ignore_batch=True)
    if labels:
      labels = tensorspec_utils.validate_and_pack(
          expected_spec=self.get_label_specification(mode),
          actual_tensors_or_spec=labels,
          ignore_batch=True)
    inference_outputs = self.inference_network_fn(features, labels, mode,
                                                  config, params)

    # After inference_fn no new variables are allowed to be added, otherwise
    # we would not initialize these variables.
    self.maybe_init_from_checkpoint()

    if mode == tf.estimator.ModeKeys.PREDICT:
      model_fn_results = self.create_export_outputs_fn(
          features, inference_outputs, mode, config, params)
      export_outputs = None
      if isinstance(model_fn_results, tuple):
        predictions = model_fn_results[0]
        export_outputs = model_fn_results[1]
      elif isinstance(model_fn_results, dict):
        export_outputs = {}
        if len(model_fn_results) == 1:
          name, output = list(model_fn_results.items())[0]
          export_outputs[name] = tf.estimator.export.RegressionOutput(output)
        export_outputs[tf.saved_model.signature_constants
                       .DEFAULT_SERVING_SIGNATURE_DEF_KEY] = (
                           tf.estimator.export.PredictOutput(model_fn_results))
        predictions = model_fn_results
      else:
        raise ValueError('The create_export_outputs_fn should return a '
                         'tuple(predictions, export_outputs) or predictions.')

      return tf.estimator.EstimatorSpec(
          mode=mode, predictions=predictions, export_outputs=export_outputs)

    train_fn_result = self.model_train_fn(features, labels, inference_outputs,
                                          mode, config, params)
    if isinstance(train_fn_result, tf.Tensor):
      train_loss = train_fn_result
      train_outputs = {}
    elif isinstance(train_fn_result, tuple):
      train_loss = train_fn_result[0]
      train_outputs = train_fn_result[1]
    else:
      raise ValueError('The model_train_fn should return a '
                       'tuple(loss, train_outputs) or loss.')

    if mode == tf.estimator.ModeKeys.TRAIN:
      # Create the tf.train.Optimizer.
      optimizer = self.create_optimizer()

      # Required for batch norm usage.
      update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
      with tf.control_dependencies(update_ops):
        train_op = self.create_train_op(train_loss, optimizer)

      self.add_summaries(features, labels, inference_outputs, train_loss,
                         train_outputs, mode, config, params)

      training_hooks = []

      # EstimatorSpec has training_chief_hooks, but TPUEstimatorSpec does not,
      # so we have to use training_hooks here and check is_chief.
      if config and config.is_chief:  # pytype: disable=attribute-error
        training_hooks.append(
            gin_utils.GinConfigSaverHook(
                config.model_dir, summarize_config=True))

      # `SyncReplicasOptimizer` needs to attach a training hook.
      if self._sync_replicas_optimizer:
        training_hooks.append(
            self._sync_replicas_optimizer.make_session_run_hook(
                config.is_chief))  # pytype: disable=attribute-error

      return tf.estimator.EstimatorSpec(
          mode=mode,
          loss=train_loss,
          train_op=train_op,
          training_hooks=training_hooks,
          scaffold=self._scaffold_fn())

    if mode == tf.estimator.ModeKeys.EVAL:
      self.add_summaries(features, labels, inference_outputs, train_loss,
                         train_outputs, mode, config, params)
      eval_metrics = self.model_eval_fn(features, labels, inference_outputs,
                                        train_loss, train_outputs, mode, config,
                                        params)
      evaluation_hooks = self.get_eval_hooks(config, params)
      if config and config.is_chief:  # pytype: disable=attribute-error
        eval_name = params.get('eval_name', 'eval')  # pytype: disable=attribute-error
        evaluation_hooks.append(
            gin_utils.GinConfigSaverHook(
                os.path.join(config.model_dir, eval_name),
                summarize_config=True))
      return tf.estimator.EstimatorSpec(
          mode=mode,
          loss=train_loss,
          eval_metric_ops=eval_metrics,
          evaluation_hooks=evaluation_hooks)

    raise ValueError('The mode {} is not supported yet.'.format(mode))

  def create_optimizer(self):
    """Create the optimizer used for training.

    This function optionally wraps the base optimizer with SyncReplicasOptimizer
    (aggregrate gradients across devices).

    Returns:
      An instance of `tf.train.Optimizer`.
    """
    config = self.get_run_config()
    optimizer = self._create_optimizer_fn()
    if self._use_avg_model_params:
      optimizer = optimizers.create_moving_average_optimizer(optimizer)
      def create_swapping_saver_scaffold():
        saver = optimizers.create_swapping_saver(optimizer)
        tf.add_to_collection(tf.GraphKeys.SAVERS, saver)
        return tf.train.Scaffold(saver=saver)
      self._scaffold_fn = create_swapping_saver_scaffold
    if (self._use_sync_replicas_optimizer and (not self.is_device_tpu) and
        config is not None and config.num_worker_replicas > 1):
      optimizer = tf.train.SyncReplicasOptimizer(
          optimizer,
          replicas_to_aggregate=config.num_worker_replicas - 1,
          total_num_replicas=config.num_worker_replicas)
      self._sync_replicas_optimizer = optimizer
    return optimizer

  def use_summaries(self, params = None):
    """Determine whether or not summaries should be used within this model.

    Note, we cannot simply use members for this operation since we reuse
    models, therefore we have to use a input/output data structure.

    Args:
      params: An optional dict of hyper parameters that will be passed into
        input_fn and model_fn. The key 'use_summaries' will be checked within
        this function and allows models to partially disable summaries during
        execution.

    Returns:
      True if summaries should be used and we are not running on TPUs otherwise
      False.
    """
    if self.is_device_tpu:
      return False
    if params is not None and not params.get('use_summaries', True):
      return False
    return True

  def get_run_config(self):
    """Get the RunConfig for Estimator model.

    Returns:
      tf.estimator.RunConfig() for this model.
    """
    return gin_configurable_run_config_cls()

  def get_tpu_run_config(self):
    """Get the TPU RunConfig for Estimator model.

    Returns:
      tf.contrib.tpu.RunConfig() for this model.
    """
    return gin_configurable_tpu_run_config_cls(
        master=FLAGS.master, tpu_config=gin_configurable_tpu_config_cls())

  @property
  def is_device_tpu(self):
    return self._device_type == DEVICE_TYPE_TPU

  @property
  def is_device_gpu(self):
    return self._device_type == DEVICE_TYPE_GPU

  @property
  def device_type(self):
    return self._device_type

  @device_type.setter
  def device_type(self, device_type):
    self._device_type = device_type
