# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

import numpy as np
import paddle_edl.distill.distill_reader as distill_reader

if __name__ == '__main__':
    # temp local test
    distill_reader.distill_worker._NOP_PREDICT_TEST = True

    # test mnist distill reader
    def _reader():
        img = np.array(
            [(i + 1) / 28.0 for i in range(28)] * 28,
            dtype=np.float32).reshape((1, 28, 28))
        label = np.array([100], dtype=np.int64)
        for i in range(24):
            yield 8 * [(img, label)]
        yield 2 * [(img, label)]

    dr = distill_reader.DistillReader(
        ins=['img', 'label'],
        predicts=['prediction'],
        conf_file='distill_reader_test_mnist_client_conf/serving_client_conf.prototxt'
    )
    dr.set_teacher_batch_size(4)
    dr.set_fixed_teacher(['127.0.0.1:9292', '127.0.0.1:9293'])
    # dr.set_dynamic_teacher(['127.0.0.1:7001'], 'DistillReaderTest', 3)

    dr.set_sample_list_generator(_reader)

    for epoch in range(300):
        for step, batch in enumerate(dr()):
            print('----step={}, predict_shape={}, predict[0]={} ----'.format(
                step, len(batch), batch[-1][-1]))
            pass
        if epoch % 10 == 0:
            print('^^^^^^^^^^^^^ epoch={} predict[0][0]={}^^^^^^^^^^^^^^'.
                  format(epoch, batch[-1][-1][0]))
