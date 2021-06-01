"""
 Copyright (c) 2019-2020 Intel Corporation
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at
      http://www.apache.org/licenses/LICENSE-2.0
 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

from typing import List

import torch

from nncf.torch.algo_selector import COMPRESSION_ALGORITHMS
from nncf.api.compression import CompressionStage
from nncf.torch.compression_method_api import PTCompressionAlgorithmController
from nncf.torch.nncf_network import NNCFNetwork
from nncf.torch.sparsity.base_algo import BaseSparsityAlgoBuilder, BaseSparsityAlgoController, SparseModuleInfo
from nncf.torch.sparsity.layers import BinaryMask
from nncf.torch.sparsity.magnitude.functions import WEIGHT_IMPORTANCE_FUNCTIONS, calc_magnitude_binary_mask
from nncf.common.sparsity.schedulers import SPARSITY_SCHEDULERS
from nncf.common.sparsity.statistics import MagnitudeSparsityStatistics
from nncf.common.statistics import NNCFStatistics
from nncf.common.sparsity.statistics import LayerThreshold
from nncf.common.batchnorm_adaptation import BatchnormAdaptationAlgorithm
from nncf.config.utils import extract_bn_adaptation_init_params


@COMPRESSION_ALGORITHMS.register('magnitude_sparsity')
class MagnitudeSparsityBuilder(BaseSparsityAlgoBuilder):
    def create_weight_sparsifying_operation(self, module, compression_lr_multiplier):
        device = module.weight.device
        return BinaryMask(module.weight.size()).to(device)

    def build_controller(self, target_model: NNCFNetwork) -> PTCompressionAlgorithmController:
        return MagnitudeSparsityController(target_model, self._sparsified_module_info, self.config)


class MagnitudeSparsityController(BaseSparsityAlgoController):
    def __init__(self, target_model: NNCFNetwork, sparsified_module_info: List[SparseModuleInfo], config):
        super().__init__(target_model, sparsified_module_info)
        self._config = config
        params = self._config.get('params', {})

        self._weight_importance_fn = WEIGHT_IMPORTANCE_FUNCTIONS[params.get('weight_importance', 'normed_abs')]
        self._mode = params.get('sparsity_level_setting_mode', 'global')
        self._scheduler = None
        sparsity_init = self._config.get('sparsity_init', 0)

        if self._mode == 'global':
            params['sparsity_init'] = sparsity_init
            scheduler_cls = SPARSITY_SCHEDULERS.get(params.get('schedule', 'polynomial'))
            self._scheduler = scheduler_cls(self, params)

        self._bn_adaptation = None

        self.set_sparsity_level(sparsity_init)

    def statistics(self, quickly_collected_only: bool = False) -> NNCFStatistics:
        model_statistics = self._calculate_sparsified_model_stats()

        threshold_statistics = []
        if self._mode == 'global':
            global_threshold = self._select_threshold(self.sparsity_rate_for_sparsified_modules(),
                                                      self.sparsified_module_info)
        for minfo in self.sparsified_module_info:
            if self._mode == 'global':
                threshold = global_threshold
            else:
                threshold = self._select_threshold(self.sparsity_rate_for_sparsified_modules(minfo), [minfo])

            threshold_statistics.append(LayerThreshold(minfo.module_name, threshold))

        stats = MagnitudeSparsityStatistics(model_statistics, threshold_statistics)

        nncf_stats = NNCFStatistics()
        nncf_stats.register('magnitude_sparsity', stats)
        return nncf_stats

    def freeze(self):
        for layer in self.sparsified_module_info:
            layer.operand.frozen = True

    def set_sparsity_level(self, sparsity_level,
                           target_sparsified_module_info: SparseModuleInfo = None,
                           run_batchnorm_adaptation: bool = False):
        if sparsity_level >= 1 or sparsity_level < 0:
            raise AttributeError(
                'Sparsity level should be within interval [0,1), actual value to set is: {}'.format(sparsity_level))
        if target_sparsified_module_info is None:
            target_sparsified_module_info_list = self.sparsified_module_info # List[SparseModuleInfo]
        else:
            target_sparsified_module_info_list = [target_sparsified_module_info]
        threshold = self._select_threshold(sparsity_level, target_sparsified_module_info_list)
        self._set_masks_for_threshold(threshold, target_sparsified_module_info_list)

        if run_batchnorm_adaptation:
            self._run_batchnorm_adaptation()

    def _select_threshold(self, sparsity_level, target_sparsified_module_info_list):
        all_weights = self._collect_all_weights(target_sparsified_module_info_list)
        if not all_weights:
            return 0.0
        all_weights_tensor, _ = torch.cat(all_weights).sort()
        threshold = all_weights_tensor[int((all_weights_tensor.size(0) - 1) * sparsity_level)].item()
        return threshold

    def _set_masks_for_threshold(self, threshold_val, target_sparsified_module_info_list):
        for layer in target_sparsified_module_info_list:
            if not layer.operand.frozen:
                layer.operand.binary_mask = calc_magnitude_binary_mask(layer.module.weight,
                                                                       self._weight_importance_fn,
                                                                       threshold_val)

    def _collect_all_weights(self, target_sparsified_module_info_list: List[SparseModuleInfo]):
        all_weights = []
        for minfo in target_sparsified_module_info_list:
            all_weights.append(self._weight_importance_fn(minfo.module.weight).view(-1))
        return all_weights

    def compression_stage(self) -> CompressionStage:
        if self._mode == 'local':
            return CompressionStage.FULLY_COMPRESSED

        if self.scheduler.current_sparsity_level == 0:
            return CompressionStage.UNCOMPRESSED
        if self.scheduler.current_sparsity_level >= self.scheduler.target_level:
            return CompressionStage.FULLY_COMPRESSED
        return CompressionStage.PARTIALLY_COMPRESSED

    def _run_batchnorm_adaptation(self):
        if self._bn_adaptation is None:
            self._bn_adaptation = BatchnormAdaptationAlgorithm(**extract_bn_adaptation_init_params(self._config))
        self._bn_adaptation.run(self.model)