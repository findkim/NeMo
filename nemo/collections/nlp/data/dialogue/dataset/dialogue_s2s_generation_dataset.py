# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
# Copyright 2019 The Google Research Authors.
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

import torch

from nemo.core.classes import Dataset


class DialogueS2SGenerationDataset(Dataset):
    def __init__(self, dataset_split: str, dialogues_processor: object, tokenizer, cfg):
        """ Constructor
        Designed for free form generation tasks such as Dialogue Response Generation 

        Args:
            dataset_split: dataset split
            dialogues_processor: dialogues processor
        """
        self.cfg = cfg
        self.input_label_type = self.cfg.input_field
        self.output_label_type = self.cfg.output_field
        self.tokenizer = tokenizer
        if not isinstance(dataset_split, str):
            dataset_split = dataset_split[0]

        self.features = dialogues_processor.get_dialog_examples(dataset_split)
        print("number of samples before filtering invalid samples: ", len(self.features))
        self.features = self.remove_invalid_samples(self.features)
        print("number of samples after filtering invalid samples: ", len(self.features))

        if self.cfg.debug_mode:
            self.features = self.features[:16]

    def remove_invalid_samples(self, features):
        valid_idxs = []
        for i in range(len(features)):
            features[i].data["labels"]["utterance"] = features[i].data["utterance"]
            all_fields = self.input_label_type.split('+') + self.output_label_type.split('+')
            all_fields_non_empty = True
            for field in all_fields:
                if not features[i].data["labels"][field] or not features[i].data["labels"][field].strip():
                    all_fields_non_empty = False
            if all_fields_non_empty:
                valid_idxs.append(i)
        return [features[i] for i in valid_idxs]

    def __len__(self):
        return len(self.features)

    def get_n_tokens_in_sentence(self, sentence):
        encodings_dict = self.tokenizer.tokenizer(
            sentence, truncation=True, max_length=self.cfg.max_seq_length, padding=False, return_tensors="pt"
        )
        output = torch.squeeze(encodings_dict['input_ids'])
        return len(output) if len(output.size()) > 0 else 0

    def default_encode(self, sentence):
        encodings_dict = self.tokenizer.tokenizer(
            sentence, truncation=True, max_length=self.cfg.max_seq_length, padding="max_length", return_tensors="pt"
        )
        input_ids = torch.squeeze(encodings_dict['input_ids'])
        attn_masks = torch.squeeze(encodings_dict['attention_mask'])
        return encodings_dict, input_ids, attn_masks

    def format_prompt(self, ex):
        '''
        Formats training prompt based on self.input_field_type

        Training example: 
            e.g. response: <response> # input_label_type = response
            e.g. utterance: <utterance> # input_label_type = utterance
            e.g. passage: <passage> utterance: <utterance> # input_label_type = passage+utterance
        '''
        ex["labels"]["utterance"] = ex["utterance"]
        parts = self.input_label_type.split('+')
        input_sentence = ' '.join([part + ': ' + ex["labels"][part] for part in parts])
        return input_sentence

    def __getitem__(self, idx: int):

        '''
        State how the input and output samples look like

        This template can be changed

        Training example: 
            e.g. INPUT - "response: <response>" OUTPUT - "<fluent_response>"  # input_label_type = response, output_label_type = fluent_response
            e.g. INPUT - "utterance: <utterance>" OUTPUT - "<response>" # input_label_type = utterance, output_label_type = response
            e.g. INPUT - "passage: <passage> utterance: <utterance>" OUTPUT - "<response>" # input_label_type = passage+utterance, output_label_type = response
        '''
        ex = self.features[idx].data

        input_sentence = self.format_prompt(ex)

        output_sentence = ex["labels"][self.output_label_type]

        _, input_ids, attn_masks = self.default_encode(input_sentence)

        _, labels, _ = self.default_encode(output_sentence)

        labels[labels == self.tokenizer.tokenizer.pad_token_id] = -100

        return input_ids, attn_masks, labels