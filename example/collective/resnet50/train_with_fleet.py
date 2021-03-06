#copyright (c) 2019 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import numpy as np
import time
import sys
import functools
import math
import json
import argparse
import functools
import subprocess
import paddle
import paddle.fluid as fluid
import utils
import models
from paddle.fluid.contrib.mixed_precision.decorator import decorate
import utils.reader_cv2 as reader
from utils.utility import add_arguments, print_arguments, check_gpu
import utils.utility as utility
from utils.learning_rate import cosine_decay_with_warmup, lr_warmup
from paddle.fluid.incubate.fleet.collective import fleet, DistributedStrategy, TrainStatus
import paddle.fluid.incubate.fleet.base.role_maker as role_maker
from paddle.fluid import compiler
import paddle.fluid.profiler as profiler
from paddle.distributed.fs_wrapper import BDFS, LocalFS

num_trainers = int(os.environ.get('PADDLE_TRAINERS_NUM', 1))
trainer_id = int(os.environ.get('PADDLE_TRAINER_ID'))

parser = argparse.ArgumentParser(description=__doc__)
add_arg = functools.partial(add_arguments, argparser=parser)

# yapf: disable
add_arg('batch_size',       int,   32,                   "Minibatch size per device.")
add_arg('total_batch_size',       int,   -1,                   "total minibatch size per device.")
add_arg('total_images',     int,   1281167,              "Training image number.")
add_arg('num_epochs',       int,   120,                  "number of epochs.")
add_arg('class_dim',        int,   1000,                 "Class number.")
add_arg('image_shape',      str,   "3,224,224",          "input image size")
add_arg('model_save_dir',   str,   "output",             "model save directory")
add_arg('with_mem_opt',     bool,  False,                "Whether to use memory optimization or not.")
add_arg('with_inplace',     bool,  False,                "Whether to use inplace memory optimization.")
add_arg('pretrained_model', str,   None,                 "Whether to use pretrained model.")
add_arg('checkpoint',       str,   None,                 "Whether to resume checkpoint.")
add_arg('hdfs_name',       str,   None,                 "hdfs_name.")
add_arg('hdfs_ugi',       str,   None,                 "hdfs_ugi.")
add_arg('lr',               float, 0.1,                  "set learning rate.")
add_arg('lr_strategy',      str,   "piecewise_decay",    "Set the learning rate decay strategy.")
add_arg('model',            str,   "SE_ResNeXt50_32x4d", "Set the network to use.")
add_arg('data_dir',         str,   "./data/ILSVRC2012/",  "The ImageNet dataset root dir.")
add_arg('fp16',             bool,  False,                "Enable half precision training with fp16." )
add_arg('use_dali',             bool,  False,            "use DALI for preprocess or not." )
add_arg('data_format',      str,   "NCHW",               "Tensor data format when training.")
add_arg('scale_loss',       float, 128.0,                  "Scale loss for fp16." )
add_arg('use_dynamic_loss_scaling',     bool,   True,    "Whether to use dynamic loss scaling.")
add_arg('l2_decay',         float, 1e-4,                 "L2_decay parameter.")
add_arg('momentum_rate',    float, 0.9,                  "momentum_rate.")
add_arg('use_label_smoothing',      bool,      False,        "Whether to use label_smoothing or not")
add_arg('label_smoothing_epsilon',      float,     0.2,      "Set the label_smoothing_epsilon parameter")
add_arg('lower_scale',      float,     0.08,      "Set the lower_scale in ramdom_crop")
add_arg('lower_ratio',      float,     3./4.,      "Set the lower_ratio in ramdom_crop")
add_arg('upper_ratio',      float,     4./3.,      "Set the upper_ratio in ramdom_crop")
add_arg('resize_short_size',      int,     256,      "Set the resize_short_size")
add_arg('use_mixup',      bool,      False,        "Whether to use mixup or not")
add_arg('mixup_alpha',      float,     0.2,      "Set the mixup_alpha parameter")
add_arg('is_distill',       bool,  False,        "is distill or not")
add_arg('profile',             bool,  False,                "Enable profiler or not." )
add_arg('fetch_steps',      int,  10,                "Enable profiler or not." )

add_arg('do_test',          bool,  False,                 "Whether do test every epoch.")
add_arg('use_gpu',          bool,  True,                 "Whether to use GPU or not.")
add_arg('fuse', bool, False,                      "Whether to use tensor fusion.")
add_arg('fuse_elewise_add_act_ops', bool, False,                      "Whether to use elementwise_act fusion.")
add_arg('fuse_bn_act_ops', bool, False,                      "Whether to use bn_act fusion.")
add_arg('nccl_comm_num',        int,  1,                  "nccl comm num")
add_arg("use_hierarchical_allreduce",     bool,   False,   "Use hierarchical allreduce or not.")
add_arg('num_threads',        int,  1,                   "Use num_threads to run the fluid program.")
add_arg('num_iteration_per_drop_scope', int,    100,      "Ihe iteration intervals to clean up temporary variables.")
add_arg('benchmark_test',          bool,  False,                 "Whether to use print benchmark logs or not.")

add_arg('use_dgc',           bool,  False,          "Whether use DGCMomentum Optimizer or not")
add_arg('rampup_begin_step', int,   5008,           "The beginning step from which dgc is implemented.")

add_arg('image_mean', nargs='+', type=float, default=[0.485, 0.456, 0.406], help="The mean of input image data")
add_arg('image_std', nargs='+', type=float, default=[0.229, 0.224, 0.225], help="The std of input image data")
add_arg('interpolation',    int,  None,                 "The interpolation mode")
add_arg('use_recompute',           bool,  False,          "Whether use Recompute Optimizer or not")

def get_momentum_optimizer(momentum_kwargs, use_dgc=False, dgc_kwargs={}):
    if not use_dgc:
        optimizer = fluid.optimizer.Momentum(**momentum_kwargs)
    else:
        dgc_kwargs.update(momentum_kwargs)
        optimizer = fluid.optimizer.DGCMomentumOptimizer(**dgc_kwargs)
    return optimizer

def optimizer_setting(params):
    ls = params["learning_strategy"]
    l2_decay = params["l2_decay"]
    momentum_rate = params["momentum_rate"]
    regularizer=fluid.regularizer.L2Decay(l2_decay)

    momentum_kwargs = {'learning_rate': None,
                       'momentum': momentum_rate,
                       'regularization': regularizer}

    use_dgc = params["use_dgc"]
    rampup_begin_step = params['rampup_begin_step']
    dgc_kwargs = {'rampup_begin_step': rampup_begin_step}

    if ls["name"] == "piecewise_decay":
        global_batch_size = ls["batch_size"] * num_trainers
        steps_per_pass = int(math.ceil(params["total_images"] * 1.0 / global_batch_size))
        print("steps_per_pass:", steps_per_pass)

        warmup_steps = steps_per_pass * 5
        passes = [30,60,80,90]
        bd = [steps_per_pass * p for p in passes]

        batch_denom = 256
        start_lr = params["lr"]
        base_lr = params["lr"] * global_batch_size / batch_denom
        lr = [base_lr * (0.1**i) for i in range(len(bd) + 1)]
        print("lr:", lr)
        lr_var = lr_warmup(fluid.layers.piecewise_decay(boundaries=bd, values=lr),\
                           warmup_steps, start_lr, base_lr)
        momentum_kwargs['learning_rate'] = lr_var

        optimizer = get_momentum_optimizer(momentum_kwargs, use_dgc, dgc_kwargs)

    elif ls["name"] == "cosine_decay":
        assert "total_images" in params
        total_images = params["total_images"]
        images_per_trainer = int(math.ceil(float(total_images) / num_trainers))
        batch_size = ls["batch_size"]
        step = int(math.ceil(float(images_per_trainer) / batch_size))
        l2_decay = params["l2_decay"]
        momentum_rate = params["momentum_rate"]
        lr = params["lr"]
        num_epochs = params["num_epochs"]

        momentum_kwargs['learning_rate'] = fluid.layers.cosine_decay(
            learning_rate=lr, step_each_epoch=step, epochs=num_epochs)

        optimizer = get_momentum_optimizer(momentum_kwargs, use_dgc, dgc_kwargs)

    elif ls["name"] == "cosine_warmup_decay":
        assert "total_images" in params
        total_images = params["total_images"]
        images_per_trainer = int(math.ceil(float(total_images) / num_trainers))
        batch_size = ls["batch_size"]
        step = int(math.ceil(float(images_per_trainer) / batch_size))
        l2_decay = params["l2_decay"]
        momentum_rate = params["momentum_rate"]
        lr = params["lr"]
        num_epochs = params["num_epochs"]

        momentum_kwargs['learning_rate'] = cosine_decay_with_warmup(
            learning_rate=lr, step_each_epoch=step, epochs=num_epochs)

        optimizer = get_momentum_optimizer(momentum_kwargs, use_dgc, dgc_kwargs)

    elif ls["name"] == "linear_decay":
        assert "total_images" in params
        total_images = params["total_images"]
        images_per_trainer = int(math.ceil(float(total_images) / num_trainers))
        batch_size = ls["batch_size"]
        step = int(math.ceil(float(images_per_trainer) / batch_size))
        num_epochs = params["num_epochs"]
        start_lr = params["lr"]
        l2_decay = params["l2_decay"]
        momentum_rate = params["momentum_rate"]
        end_lr = 0
        total_step = step * num_epochs
        lr = fluid.layers.polynomial_decay(
            start_lr, total_step, end_lr, power=1)
        momentum_kwargs['learning_rate'] = lr
        optimizer = get_momentum_optimizer(momentum_kwargs, use_dgc, dgc_kwargs)
    elif ls["name"] == "adam":
        if use_dgc:
            print("Warning: Adam is not support dgc. So will not use dgc")
        lr = params["lr"]
        optimizer = fluid.optimizer.Adam(learning_rate=lr)
    elif ls["name"] == "rmsprop_cosine":
        if use_dgc:
            print("Warning: Adam is not support dgc. So will not use dgc")
        assert "total_images" in params
        total_images = params["total_images"]
        images_per_trainer = int(math.ceil(float(total_images) / num_trainers))
        batch_size = ls["batch_size"]
        step = int(math.ceil(float(images_per_trainer) / batch_size))
        l2_decay = params["l2_decay"]
        momentum_rate = params["momentum_rate"]
        lr = params["lr"]
        num_epochs = params["num_epochs"]
        optimizer = fluid.optimizer.RMSProp(
            learning_rate=fluid.layers.cosine_decay(
                learning_rate=lr, step_each_epoch=step, epochs=num_epochs),
            momentum=momentum_rate,
            regularization=fluid.regularizer.L2Decay(l2_decay),
            # RMSProp Optimizer: Apply epsilon=1 on ImageNet.
            epsilon=1)
    else:
        lr = params["lr"]
        momentum_kwargs['learning_rate'] = lr
        optimizer = get_momentum_optimizer(momentum_kwargs, use_dgc, dgc_kwargs)

    return optimizer

def calc_loss(epsilon,label,class_dim,softmax_out,use_label_smoothing):
    if use_label_smoothing:
        label_one_hot = fluid.layers.one_hot(input=label, depth=class_dim)
        smooth_label = fluid.layers.label_smooth(label=label_one_hot, epsilon=epsilon, dtype="float32")
        loss = fluid.layers.cross_entropy(input=softmax_out, label=smooth_label, soft_label=True)
    else:
        print("Using fluid.layers.cross_entropy.")
        loss = fluid.layers.cross_entropy(input=softmax_out, label=label)
    return loss


def net_config(image, model, args, is_train, label=0, y_a=0, y_b=0, lam=0.0, data_format="NCHW"):
    model_list = [m for m in dir(models) if "__" not in m]
    assert args.model in model_list, "{} is not lists: {}".format(args.model,
                                                                  model_list)
    class_dim = args.class_dim
    model_name = args.model
    use_mixup = args.use_mixup
    use_label_smoothing = args.use_label_smoothing
    epsilon = args.label_smoothing_epsilon

    if not args.is_distill:
        out = model.net(input=image, args=args, class_dim=class_dim, data_format=data_format)
        if is_train:
            if use_mixup:
                softmax_out = fluid.layers.softmax(out, use_cudnn=False)
                loss_a = calc_loss(epsilon,y_a,class_dim,softmax_out,use_label_smoothing)
                loss_b = calc_loss(epsilon,y_b,class_dim,softmax_out,use_label_smoothing)
                loss_a_mean = fluid.layers.mean(x = loss_a)
                loss_b_mean = fluid.layers.mean(x = loss_b)
                cost = lam * loss_a_mean + (1 - lam) * loss_b_mean
                avg_cost = fluid.layers.mean(x=cost)
                return avg_cost
            else:
                print("Use fluid.layers.softmax_with_cross_entropy.")
                cost, softmax_out = fluid.layers.softmax_with_cross_entropy(
                    out, label, return_softmax=True)
        else:
            cost, softmax_out = fluid.layers.softmax_with_cross_entropy(
                out, label, return_softmax=True)
    else:
        out1, out2 = model.net(input=image, args=args, class_dim=args.class_dim, data_format=data_format)
        softmax_out1, softmax_out = fluid.layers.softmax(out1), fluid.layers.softmax(out2)
        smooth_out1 = fluid.layers.label_smooth(label=softmax_out1, epsilon=0.0, dtype="float32")
        cost = fluid.layers.cross_entropy(input=softmax_out, label=smooth_out1, soft_label=True)

    avg_cost = fluid.layers.mean(cost)
    acc_top1 = fluid.layers.accuracy(input=softmax_out, label=label, k=1)
    acc_top5 = fluid.layers.accuracy(input=softmax_out, label=label, k=5)
    return avg_cost, acc_top1, acc_top5

def build_program(is_train, main_prog, startup_prog, args, dist_strategy=None, data_layout="NCHW"):
    model_name = args.model
    model_list = [m for m in dir(models) if "__" not in m]
    assert model_name in model_list, "{} is not in lists: {}".format(args.model,
                                                                     model_list)
    model = models.__dict__[model_name]()
    with fluid.program_guard(main_prog, startup_prog):
        use_mixup = args.use_mixup
        data_loader, data = utility.create_data_loader(is_train, args, data_layout=data_layout)

        with fluid.unique_name.guard():
            if is_train and  use_mixup:
                image, y_a, y_b, lam = data[0], data[1], data[2], data[3]
                avg_cost = net_config(image=image, y_a=y_a, y_b=y_b, lam=lam, model=model,
                                      args=args, label=0, is_train=True, data_format=data_layout)
                avg_cost.persistable = True
                build_program_out = [data_loader, avg_cost]
            else:
                image, label = data[0], data[1],
                avg_cost, acc_top1, acc_top5 = net_config(image, model, args,
                                                          label=label, is_train=is_train, data_format=data_layout)
                avg_cost.persistable = True
                acc_top1.persistable = True
                acc_top5.persistable = True
                build_program_out = [data_loader, avg_cost, acc_top1, acc_top5]

            if is_train:
                params = model.params
                params["total_images"] = args.total_images
                params["lr"] = args.lr
                params["num_epochs"] = args.num_epochs
                params["learning_strategy"]["batch_size"] = args.batch_size
                params["learning_strategy"]["name"] = args.lr_strategy
                params["l2_decay"] = args.l2_decay
                params["momentum_rate"] = args.momentum_rate
                params["use_dgc"] = args.use_dgc
                params["rampup_begin_step"] = args.rampup_begin_step

                optimizer = optimizer_setting(params)
                global_lr = optimizer._global_learning_rate()
                if args.fp16:
                    optimizer = fluid.contrib.mixed_precision.decorate(optimizer,
                                                                       init_loss_scaling=args.scale_loss,
                                                                       use_dynamic_loss_scaling=args.use_dynamic_loss_scaling)
                if args.use_recompute:
                    dist_strategy.forward_recompute = True
                    dist_strategy.enable_sequential_execution=True
                    dist_strategy.recompute_checkpoints = model.checkpoints
                dist_optimizer = fleet.distributed_optimizer(optimizer, strategy=dist_strategy)
                _, param_grads = dist_optimizer.minimize(avg_cost)

                global_lr.persistable=True
                build_program_out.append(global_lr)

    return build_program_out

def get_device_num():
    """
    # NOTE(zcd): for multi-processe training, each process use one GPU card.
    if num_trainers > 1 : return 1
    visible_device = os.environ.get('CUDA_VISIBLE_DEVICES', None)
    if visible_device:
        device_num = len(visible_device.split(','))
    else:
        device_num = subprocess.check_output(['nvidia-smi','-L']).decode().count('\n')
    """
    device_num = fluid.core.get_cuda_device_count()
    return device_num

def train(args):
    # parameters from arguments
    model_name = args.model
    #checkpoint = args.checkpoint
    pretrained_model = args.pretrained_model
    model_save_dir = args.model_save_dir
    use_mixup = args.use_mixup
    use_ngraph = os.getenv('FLAGS_use_ngraph')

    startup_prog = fluid.Program()
    train_prog = fluid.Program()
    test_prog = fluid.Program()

    if args.total_batch_size > 0:
        args.batch_size = int(args.total_batch_size / num_trainers)

    exec_strategy = fluid.ExecutionStrategy()
    exec_strategy.num_threads = args.num_threads
    exec_strategy.num_iteration_per_drop_scope = args.num_iteration_per_drop_scope

    dist_strategy = DistributedStrategy()
    dist_strategy.exec_strategy = exec_strategy
    dist_strategy.enable_inplace = args.with_inplace
    if not args.fuse:
        dist_strategy.fuse_all_reduce_ops = False
    dist_strategy.nccl_comm_num = args.nccl_comm_num
    dist_strategy.fuse_elewise_add_act_ops=args.fuse_elewise_add_act_ops
    dist_strategy.fuse_bn_act_ops = args.fuse_bn_act_ops

    role = role_maker.PaddleCloudRoleMaker(is_collective=True)
    fleet.init(role)

    b_out = build_program(
                     is_train=True,
                     main_prog=train_prog,
                     startup_prog=startup_prog,
                     args=args,
                     dist_strategy=dist_strategy,
                     data_layout=args.data_format)
    if use_mixup:
        train_data_loader, train_cost, global_lr = b_out[0], b_out[1], b_out[2]
        train_fetch_vars = [train_cost, global_lr]
        train_fetch_list = []
        for var in train_fetch_vars:
            var.persistable=True
            train_fetch_list.append(var.name)

    else:
        train_data_loader, train_cost, train_acc1, train_acc5, global_lr = b_out[0],b_out[1],b_out[2],b_out[3],b_out[4]
        train_fetch_vars = [train_cost, train_acc1, train_acc5, global_lr]
        train_fetch_list = []
        for var in train_fetch_vars:
            var.persistable=True
            train_fetch_list.append(var.name)
        train_fetch_list.append("@LR_DECAY_COUNTER@")

    train_prog = fleet.main_program

    b_out_test = build_program(
                     is_train=False,
                     main_prog=test_prog,
                     startup_prog=startup_prog,
                     args=args,
                     dist_strategy=dist_strategy,
                     data_layout=args.data_format)
    test_data_loader, test_cost, test_acc1, test_acc5 = b_out_test[0],b_out_test[1],b_out_test[2],b_out_test[3]

    test_prog = test_prog.clone(for_test=True)
    test_prog = compiler.CompiledProgram(test_prog).with_data_parallel(loss_name=test_cost.name, exec_strategy=exec_strategy)

    gpu_id = int(os.environ.get('FLAGS_selected_gpus', 0))
    place = fluid.CUDAPlace(gpu_id) if args.use_gpu else fluid.CPUPlace()
    exe = fluid.Executor(place)
    exe.run(startup_prog)

    fs=LocalFS()
    if args.hdfs_name and args.hdfs_ugi:
        fs=BDFS(args.hdfs_name, args.hdfs_ugi,20*60*1000, 3 * 1000)

    train_status =TrainStatus()
    if args.checkpoint is not None:
        tmp_s = fleet.load_check_point(exe, args.checkpoint, fs=fs, trainer_id=trainer_id)#, main_program=fleet._origin_program)
        if tmp_s is not None:
            train_status = tmp_s

        scope = fluid.global_scope()
        print("global lr:", np.array(scope.find_var(global_lr.name).get_tensor()))
        print("global step:", np.array(scope.find_var("@LR_DECAY_COUNTER@").get_tensor()))

    if pretrained_model:
        def if_exist(var):
            return os.path.exists(os.path.join(pretrained_model, var.name))

        fluid.io.load_vars(
            exe, pretrained_model, main_program=train_prog, predicate=if_exist)

    if args.use_gpu:
        device_num = get_device_num()
    else:
        device_num = 1

    train_batch_size = args.batch_size
    print("train_batch_size: %d device_num:%d total_batch_size:%d" % (train_batch_size, device_num, args.total_batch_size))

    test_batch_size = args.batch_size
    # NOTE: the order of batch data generated by batch_reader
    # must be the same in the respective processes.
    #shuffle_seed = 1 if num_trainers > 1 else None

    if args.use_dali:
        import dali
        train_iter = dali.train(settings=args,
                                pass_id_as_seed=train_status.next(),
                                trainer_id=trainer_id, trainers_num=num_trainers,
                                gpu_id=gpu_id, data_layout=args.data_format)
    else:
        train_reader = reader.train(settings=args, data_dir=args.data_dir,
                                    pass_id_as_seed=train_status.next(), data_layout=args.data_format, threads=10)
        train_batch_reader=paddle.batch(train_reader, batch_size=train_batch_size)

        test_reader = reader.val(settings=args, data_dir=args.data_dir, data_layout=args.data_format, threads=10)
        test_batch_reader=paddle.batch(test_reader, batch_size=test_batch_size)

        places = place
        if num_trainers <= 1 and args.use_gpu:
            places = fluid.framework.cuda_places()

        train_data_loader.set_sample_list_generator(train_batch_reader, places)
        test_data_loader.set_sample_list_generator(test_batch_reader, place)

    test_fetch_vars = [test_cost, test_acc1, test_acc5]
    test_fetch_list = []
    for var in test_fetch_vars:
        var.persistable=True
        test_fetch_list.append(var.name)

    train_exe = exe

    params = models.__dict__[args.model]().params

    train_speed_list = []
    acc1_logs = []
    acc5_logs = []
    pass_id=None
    for pass_id in range(train_status.next(), params["num_epochs"]):
        train_info = [[], [], []]
        test_info = [[], [], []]
        train_begin=time.time()
        batch_id = 0
        time_record=[]

        if not args.use_dali:
            train_iter = train_data_loader()

        for data in train_iter:
            t1 = time.time()

            if batch_id % args.fetch_steps != 0:
                train_exe.run(train_prog, feed=data)
            else:
                if use_mixup:
                    loss, lr = train_exe.run(train_prog, feed=data, fetch_list=train_fetch_list)
                else:
                    loss, acc1, acc5, lr, global_step = train_exe.run(train_prog,  feed=data,  fetch_list=train_fetch_list)
                    acc1 = np.mean(np.array(acc1))
                    acc5 = np.mean(np.array(acc5))
                    train_info[1].append(acc1)
                    train_info[2].append(acc5)
                    global_step = np.array(global_step)

            t2 = time.time()
            period = t2 - t1
            time_record.append(period)

            if args.profile and batch_id == 100:
                print("begin profiler")
                if trainer_id == 0:
                    profiler.start_profiler("All")
            elif args.profile and batch_id == 105:
                print("begin to end profiler")
                if trainer_id == 0:
                    profiler.stop_profiler("total", "./profile_pass_%d" % (pass_id))
                print("end profiler break!")
                args.profile=False

            if batch_id % args.fetch_steps == 0:
                loss = np.mean(np.array(loss))
                train_info[0].append(loss)
                lr = np.mean(np.array(lr))
                period = np.mean(time_record)
                speed = args.batch_size * 1.0 / period
                time_record=[]
                if use_mixup:
                    print("Pass {0}, trainbatch {1}, loss {2}, lr {3}, time {4}, speed {5}"
                          .format(pass_id, batch_id, "%.5f"%loss, "%.5f" %lr, "%2.4f sec" % period, "%.2f" % speed))
                else:
                    print("Pass {0}, trainbatch {1}, loss {2}, \
                        acc1 {3}, acc5 {4}, lr {5}, time {6}, speed {7}"
                          .format(pass_id, batch_id, "%.5f"%loss, "%.5f"%acc1, "%.5f"%acc5, "%.5f" %
                                  lr, "%2.4f sec" % period, "%.2f" % speed))
                    print("global_step 2:", global_step)
                sys.stdout.flush()
            batch_id += 1

        if args.use_dali:
            train_iter.reset()

        train_loss = np.array(train_info[0]).mean()
        if not use_mixup:
            train_acc1 = np.array(train_info[1]).mean()
            train_acc5 = np.array(train_info[2]).mean()
        train_end=time.time()
        train_speed = (batch_id * train_batch_size) / (train_end - train_begin)
        train_speed_list.append(train_speed)

        if trainer_id == 0:
            saved_status = TrainStatus(pass_id)
            if args.checkpoint:
                if not os.path.isdir(args.checkpoint):
                    os.makedirs(args.checkpoint)

                print("save_check_point:{}".format(args.checkpoint))
                fleet.save_check_point(executor=exe, train_status=saved_status,
                    path=args.checkpoint, fs=fs)#, main_program=fleet._origin_program)


        if trainer_id == 0 and (args.do_test or (pass_id + 1) == params["num_epochs"]):
            if args.use_dali:
                test_iter = dali.val(settings=args, trainer_id=trainer_id, trainers_num=num_trainers,
                                 gpu_id=gpu_id, data_layout=args.data_format)
            else:
                test_iter = test_data_loader()

            test_batch_id = 0
            for data in test_iter:
                t1 = time.time()
                loss, acc1, acc5 = exe.run(program=test_prog,
                                           feed=data,
                                           fetch_list=test_fetch_list)
                t2 = time.time()
                period = t2 - t1
                loss = np.mean(loss)
                acc1 = np.mean(acc1)
                acc5 = np.mean(acc5)
                test_info[0].append(loss)
                test_info[1].append(acc1)
                test_info[2].append(acc5)

                if test_batch_id % 10 == 0:
                    test_speed = test_batch_size * 1.0 / period
                    print("Pass {0},testbatch {1},loss {2}, \
                        acc1 {3},acc5 {4},time {5},speed {6}"
                        .format(pass_id, test_batch_id, "%.5f"%loss,"%.5f"%acc1, "%.5f"%acc5,
                                "%2.2f sec" % period, "%.2f" % test_speed))
                    sys.stdout.flush()
                test_batch_id += 1

            if args.use_dali:
                test_iter.reset()
                del test_iter

            test_loss = np.array(test_info[0]).mean()
            test_acc1 = np.array(test_info[1]).mean()
            test_acc5 = np.array(test_info[2]).mean()

            acc1_logs.append(test_acc1)
            acc5_logs.append(test_acc5)

            if use_mixup:
                print("End pass {0}, train_loss {1}, test_loss {2}, test_acc1 {3}, test_acc5 {4}, speed {5}".format(
                      pass_id, "%.5f"%train_loss, "%.5f"%test_loss, "%.5f"%test_acc1, "%.5f"%test_acc5,
                      "%.2f" % train_speed))
            else:
                print("End pass {0}, train_loss {1}, train_acc1 {2}, train_acc5 {3}, "
                  "test_loss {4}, test_acc1 {5}, test_acc5 {6}, speed {7}".format(
                      pass_id, "%.5f"%train_loss, "%.5f"%train_acc1, "%.5f"%train_acc5, "%.5f"%test_loss,
                      "%.5f"%test_acc1, "%.5f"%test_acc5, "%.2f" % train_speed))
        else:
            if use_mixup:
                print("End pass {0}, train_loss {1}, speed {2}".format(pass_id, "%.5f"%train_loss, "%.2f" % train_speed))
            else:
                print("End pass {0}, train_loss {1}, train_acc1 {2}, train_acc5 {3}, ""speed {4}".format(
                    pass_id, "%.5f"%train_loss, "%.5f"%train_acc1, "%.5f"%train_acc5, "%.2f" % train_speed))

        sys.stdout.flush()


    # save in last epoch
    if trainer_id == 0 and pass_id is not None:
        model_path = os.path.join(model_save_dir + '/' + model_name, str(pass_id))
        if not os.path.isdir(model_path):
            os.makedirs(model_path)

        fluid.io.save_persistables(exe, model_path, main_program=fleet._origin_program)

        if args.benchmark_test:
            if not os.path.isdir("./benchmark_logs/"):
                os.makedirs("./benchmark_logs/")
            with open("./benchmark_logs/log_%d" % trainer_id, 'w') as f:
                result = dict()
                result['0'] = dict()
                result['0']['acc1'] = test_acc1
                result['0']['acc5'] = test_acc5
                result['0']['result_log'] = dict()
                result['0']['result_log']['acc1'] = acc1_logs
                result['0']['result_log']['acc5'] = acc5_logs
                # maximum speed of all epochs
                result['1'] = max(train_speed_list) * num_trainers
                result['14'] = args.batch_size

                print(str(result))
                f.writelines(str(result))


def print_paddle_environments():
    print('--------- Configuration Environments -----------')
    #print("Devices per node: %d" % DEVICE_NUM)
    for k in os.environ:
        if "PADDLE_" in k or "FLAGS_" in k:
            print("%s: %s" % (k, os.environ[k]))
    print('------------------------------------------------')


def main():
    args = parser.parse_args()
    # this distributed benchmark code can only support gpu environment.
    assert args.use_gpu, "only for gpu implementation."
    if args.use_dgc:
        if args.fuse:
            print("Warning: Use dgc must close fuse for now, so code will set fuse=False")
            args.fuse = False
        if args.fp16:
            print("Warning: DGC unsupport fp16 for now, so code will set fp16=False")
            args.fp16 = False
    # fuse_bn_act_ops has bug when use fp32, so close it when use fp32
    if not args.fp16:
        args.fuse_bn_act_ops = False
    print_arguments(args)
    print_paddle_environments()
    check_gpu(args.use_gpu)
    train(args)

if __name__ == '__main__':
    main()
