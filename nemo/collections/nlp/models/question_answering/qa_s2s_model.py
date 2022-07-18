# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
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

import collections
from typing import List, Optional

import torch
from omegaconf import DictConfig, OmegaConf, open_dict
from pytorch_lightning import Trainer
from torch.cuda.amp import autocast
from transformers import AutoModelForSeq2SeqLM

from nemo.collections.nlp.data.question_answering.data_processor.qa_processing import QAProcessor
from nemo.collections.nlp.data.question_answering.dataset.qa_s2s_dataset import S2SQADataset
from nemo.collections.nlp.metrics.qa_metrics import QAMetrics
from nemo.collections.nlp.models.language_modeling.megatron_t5_model import MegatronT5Model
from nemo.collections.nlp.models.question_answering.qa_base_model import BaseQAModel
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.utils import logging


class S2SQAModel(BaseQAModel):
    def __init__(self, cfg: DictConfig, trainer: Trainer = None):

        self.cfg = cfg

        if self.cfg.library == "huggingface":
            self.setup_tokenizer(cfg.tokenizer)
        elif self.cfg.library == "megatron":
            # supporting MegatronT5Model in precision = fp16
            t5_cfg = MegatronT5Model.restore_from(
                restore_path=cfg.language_model.lm_checkpoint, trainer=trainer, return_config=True
            )
            # Override the T5 configuration with the one from the config file.
            OmegaConf.set_struct(t5_cfg, True)
            with open_dict(t5_cfg):
                t5_cfg.masked_softmax_fusion = False
                t5_cfg.precision = 16

            language_model = MegatronT5Model.restore_from(
                restore_path=cfg.language_model.lm_checkpoint, trainer=trainer, override_config_path=t5_cfg
            )
            self.tokenizer = language_model.tokenizer

        super().__init__(cfg=cfg, trainer=trainer, no_lm_init=True)

        if self.cfg.library == "huggingface":
            self.language_model = AutoModelForSeq2SeqLM.from_pretrained(cfg.language_model.pretrained_model_name)
            self.language_model.resize_token_embeddings(len(self.tokenizer.tokenizer))
            if self.cfg.language_model.lm_checkpoint:
                self.language_model.load_state_dict(torch.load(self.cfg.language_model.lm_checkpoint))
        elif self.cfg.library == "megatron":
            self.language_model = language_model

    def training_step(self, batch, batch_idx):
        input_ids, input_attn_mask, unique_ids, labels = batch
        loss, _ = self.forward(input_ids, input_attn_mask, labels)
        lr = self._optimizer.param_groups[0]['lr']

        self.log('lr', lr, prog_bar=True)
        self.log("train_loss", loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        
        return {'loss': loss}

    def validation_step(self, batch, batch_idx):
        prefix = "test" if self.trainer.testing else "val"

        input_ids, input_attn_mask, unique_ids, labels = batch
        loss, per_sample_perplexity = self.forward(input_ids, input_attn_mask, labels)
        generated_answers = self._generate_candidates(input_ids, input_attn_mask)

        labels[labels == -100] = self.tokenizer.tokenizer.pad_token_id

        return {
            "unique_ids": unique_ids,
            f"{prefix}_loss": loss,
            "per_sample_perplexity": per_sample_perplexity,
            "input": self.tokenizer.tokenizer.batch_decode(input_ids, skip_special_tokens=True),
            "ground_truth_answers": self.tokenizer.tokenizer.batch_decode(labels, skip_special_tokens=True),
            "generated_answers": generated_answers,
        }

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def validation_epoch_end(self, outputs):
        prefix = "test" if self.trainer.testing else "val"

        loss_terms = [x[f"{prefix}_loss"] for x in outputs]
        generated_answers, unique_ids, per_sample_perplexity = QAMetrics.convert_dict_outputs_to_lists(
            outputs, ["generated_answers", "unique_ids", "per_sample_perplexity"]
        )

        avg_loss = torch.stack(loss_terms).mean()

        eval_dataset = self._test_dl.dataset if self.trainer.testing else self._validation_dl.dataset
        eval_results, _, _ = self.evaluate(
            eval_dataset.features, eval_dataset.examples, unique_ids, per_sample_perplexity, generated_answers,
        )

        self.log(f'{prefix}_loss', avg_loss)
        for eval_key in eval_results:
            logging.info(f"{prefix} {eval_key}: {eval_results[eval_key]}")
            self.log(f"{prefix}_{eval_key}", eval_results[eval_key])

    def test_epoch_end(self, outputs):
        self.validation_epoch_end(outputs)

    @typecheck()
    def forward(self, input_ids, input_attn_mask, labels):
        loss, per_sample_perplexity = None, None
        if self.cfg.library == "huggingface":
            with autocast(enabled=False):
                output = self.language_model(input_ids=input_ids, attention_mask=input_attn_mask, labels=labels)
            loss = output['loss']
            lm_logits = output['logits']
            per_sample_perplexity = self._get_per_sample_perplexity(lm_logits, labels)

        elif self.cfg.library == "megatron":
            labels = torch.where(labels != -100, labels, torch.zeros_like(labels))
            output_attn_masks = torch.where(labels > 0, torch.ones_like(labels), torch.zeros_like(labels))
            unmasked_unreduced_loss = self.language_model(
                input_ids, labels[:, :-1], input_attn_mask, output_attn_masks[:, :-1], lm_labels=labels[:, 1:],
            )
            loss = self.language_model.loss_func(output_attn_masks[:, 1:], unmasked_unreduced_loss)
            per_sample_perplexity = torch.exp(unmasked_unreduced_loss)

        return loss, per_sample_perplexity

    @torch.no_grad()
    def inference(
        self,
        file: str,
        batch_size: int = 1,
        num_samples: int = -1,
        output_prediction_file: Optional[str] = None,
        output_nbest_file: Optional[str] = None,
    ):
        all_predictions = []
        mode = self.training
        device = "cuda" if isinstance(self.trainer.device_ids, list) else "cpu"
        if self.cfg.library == "huggingface":
            try:
                # switch model to evaluation mode
                self.eval()
                self.to(device)
                logging_level = logging.get_verbosity()
                logging.set_verbosity(logging.WARNING)

                inference_dl = self.setup_inference_data(file, batch_size=batch_size, num_samples=num_samples)

                outputs = self._inference(inference_dl, device)
                generated_answers, unique_ids, per_sample_perplexity = QAMetrics.convert_dict_outputs_to_lists(
                    outputs, ["generated_answers", "unique_ids", "per_sample_perplexity"]
                )
                all_predictions, all_nbest_predictions = self._get_predictions(
                    inference_dl.dataset.features,
                    inference_dl.dataset.examples,
                    unique_ids,
                    per_sample_perplexity,
                    generated_answers,
                )

                if output_prediction_file:
                    QAMetrics.dump_predicted_answers_to_file(
                        output_prediction_file,
                        inference_dl.dataset.examples,
                        all_predictions
                    )

                if output_nbest_file:
                    QAMetrics.dump_nbest_predictions_to_file(
                        output_nbest_file,
                        inference_dl.dataset.examples,
                        all_nbest_predictions,
                        keys_to_dump=["generated_text", "perplexity"],
                    )

            finally:
                # set mode back to its original value
                self.train(mode=mode)
                logging.set_verbosity(logging_level)

        elif self.cfg.library == 'megatron':
            raise ValueError("Megatron Inference is not supported by S2SQAModel")

        return all_predictions, all_nbest_predictions

    def evaluate(
        self, features, examples, unique_ids, per_sample_perplexity, generated_texts,
    ):
        all_predictions, all_nbest_json = self._get_predictions(
            features, examples, unique_ids, per_sample_perplexity, generated_texts,
        )

        eval_results = QAMetrics.evaluate_predictions(examples, all_predictions)

        return eval_results, all_predictions, all_nbest_json

    def _setup_dataloader_from_config(self, cfg: DictConfig, mode: str):
        processor = QAProcessor(cfg.file, mode)

        dataset = S2SQADataset(
            data_file=cfg.file,
            processor=processor,
            tokenizer=self.tokenizer,
            keep_doc_spans=self._cfg.dataset.keep_doc_spans,
            doc_stride=self._cfg.dataset.doc_stride,
            max_query_length=self._cfg.dataset.max_query_length,
            max_seq_length=self._cfg.dataset.max_seq_length,
            max_answer_length=self._cfg.dataset.max_answer_length,
            check_if_answer_in_context=self._cfg.dataset.check_if_answer_in_context,
            num_samples=cfg.num_samples,
            mode=mode,
            use_cache=self._cfg.dataset.use_cache,
        )

        data_loader = torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=cfg.batch_size,
            collate_fn=dataset.collate_fn,
            drop_last=cfg.drop_last,
            shuffle=cfg.shuffle,
            num_workers=cfg.num_workers,
            pin_memory=cfg.pin_memory,
        )

        return data_loader

    def _get_predictions(
        self, features, examples: List, unique_ids: List[int], per_sample_perplexity: List, generated_texts: List,
    ):

        unique_id_to_pos = {}
        for index, unique_id in enumerate(unique_ids):
            unique_id_to_pos[unique_id] = index

        example_index_to_features = collections.defaultdict(list)
        for feature in features:
            example_index_to_features[feature.example_index].append(feature)

        _PrelimPrediction = collections.namedtuple(
            "PrelimPrediction", ["feature_index", "perplexity", "generated_text"]
        )

        all_predictions = collections.OrderedDict()
        all_nbest_json = collections.OrderedDict()
        for (example_index, example) in enumerate(examples):

            # finish this loop if we went through all batch examples
            if example_index >= len(unique_ids):
                break

            curr_features = example_index_to_features[example_index]
            prelim_predictions = []
            for (feature_index, feature) in enumerate(curr_features):
                pos = unique_id_to_pos[feature.unique_id]
                curr_perplexity = per_sample_perplexity[pos]
                curr_generated_text = generated_texts[pos]
                prelim_prediction = _PrelimPrediction(feature_index, curr_perplexity, curr_generated_text)
                prelim_predictions.append(prelim_prediction)

            prelim_predictions = sorted(prelim_predictions, key=lambda x: x.perplexity)
            all_predictions[example.qas_id] = prelim_predictions[0].generated_text
            all_nbest_json[example.qas_id] = [pred._asdict() for pred in prelim_predictions]

        return all_predictions, all_nbest_json

    def _inference(self, inference_dl, device):
        outputs = []
        for i, batch in enumerate(inference_dl):

            # get predictions
            input_ids, input_attn_mask, unique_ids = batch
            input_ids, input_attn_mask = (tensor.to(device) for tensor in [input_ids, input_attn_mask])
            generated_texts = self._generate_candidates(input_ids, input_attn_mask)

            labels = self._prep_inference_labels(generated_texts, device)
            _, per_sample_perplexity = self.forward(input_ids, input_attn_mask, labels)
            labels[labels == -100] = self.tokenizer.tokenizer.pad_token_id

            outputs.append(
                {
                    "unique_ids": unique_ids,
                    "per_sample_perplexity": per_sample_perplexity,
                    "generated_answers": generated_texts,
                }
            )

        return outputs

    def _prep_inference_labels(self, generated_texts, device):
        encoded_output_dict = self.tokenizer.tokenizer(
            generated_texts,
            truncation=True,
            max_length=self._cfg.dataset.max_answer_length,
            padding="max_length",
            return_tensors="pt",
        )
        input_ids = encoded_output_dict["input_ids"].to(device)
        labels = torch.squeeze(input_ids)
        labels[labels == self.tokenizer.tokenizer.pad_token_id] = -100
        if len(labels.shape) == 1:
            labels = torch.unsqueeze(labels, 0)
        labels = labels.to(device)

        return labels

    def _generate_candidates(self, input_ids, input_attn_mask):
        num_tokens_to_generate = self.cfg.tokens_to_generate

        if self.cfg.library == "huggingface":
            param_dict = {
                "input_ids": input_ids,
                "attention_mask": input_attn_mask,
                "max_length": num_tokens_to_generate,
            }
            generated_tokens = self.language_model.generate(**param_dict)
            generated_answers = self.tokenizer.tokenizer.batch_decode(generated_tokens, skip_special_tokens=True,)
            generated_answers = [ans.strip() for ans in generated_answers]

        elif self.cfg.library == 'megatron':
            raise ValueError("Megatron Generation is not supported by S2SQAModel")

        return generated_answers

    @classmethod
    def list_available_models(cls) -> Optional[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.

        Returns:
            List of available pre-trained models.
        """
        result = []
        return result
