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

from paddle_edl.utils.utils import get_extern_ip, logger
from paddle_edl.utils.utils import get_gpus


class JobEnv(object):
    def _get_ports(self, args):
        if self._job_env.run_platform == "PADDLE_CLOUD":
            ports = os.getenv("PADDLE_TRAINER_PORTS", "")
            self._trainer_ports = ports.split(",")

            assert len(ports) >= len(self._gpus), \
                "port num:{} must large than gpus:{}".format(len(self._trainer_ports), len(self._gpus))
            logger.info("get ports from env:{}".format(self._trainer_ports))
        else:
            self._trainer_ports = utils.find_free_ports(len(_gpus))
            logger.info("get ports from unused:{}".format(self._trainer_ports))

    def _get_hdfs(self, args):
        # hdfs
        if args.hdfs_home:
            self._hdfs_home = args.hdfs_home
        else:
            self._hdfs_home = os.getenv("PADDLE_EDL_HDFS_HOME", "")

        if args.hdfs_name:
            self._hdfs_name = args.hdfs_name
        else:
            self._hdfs_name = os.getenv("PADDLE_EDL_HDFS_NAME", "")

        if args.hdfs_path:
            self._hdfs_name = args.hdfs_path
        else:
            self._hdfs_path = os.getenv("PADDLE_EDL_HDFS_PATH", "")

        if args.hdfs_ugi:
            self._hdfs_ugi = args.hdfs_ugi
        else:
            self._hdfs_ugi = os.getenv("PADDLE_EDL_HDFS_UGI", "")

    def _get_nodes_ranges(self, args):
        # nodes range
        if args.nodes_range:
            self._nodes_range = args.nodes_range
        else:
            self._nodes_range = os.getenv("PADDLE_EDL_NODES_RANGE", "")

        assert self._nodes_range is not None, "nodes range must set"
        a = self._nodes_range.split(":")
        assert len(a) == 2, "nodes_range not a valid format:{}".format(
            self._nodes_range)
        self._min_nodes = a[0]
        self._max_nodes = a[1]

    def _get_gpus(self, args):
        # selected gpus
        self._gpus = utils.get_gpus(None)

        # proc per node
        if args.nproc_per_node:
            self._nproc_per_node = args.nproc_per_node
        else:
            nproc_per_node = os.getenv("PADDLE_EDL_NPROC_PERNODE", "")
            if nproc_per_node is None:
                self.nproc_per_node = len(self._gpus)
            else:
                self._nproc_per_node = int(nproc_per_node)

        assert len(
            self._gpus
        ) >= self._nproc_per_node, "gpu's num must larger than procs need to run"

    def __init__(self, args):
        # run platform
        self._platform = os.get_env("PADDLE_RUNNING_PLATFORM", "")

        # job_id
        if args.job_id:
            self._job_id = args.job_id
        else:
            self._job_id = os.getenv("PADDLE_JOB_ID", "")
        assert self._job_id, "job_id must has valid value "

        # etcd
        if args.etcd_endpoints:
            self._etcd_endpoints = args.etcd_endpoints
        else:
            self._etcd_endpoints = os.getenv("PADDLE_ETCD_ENPOINTS", "")
        assert self._etcd_endpoints, "etcd_endpoints must has valid value "

        self._ce_test = int(os.getenv("PADDLE_EDL_ONLY_FOR_CE_TEST", "0"))
        self._get_hdfs(args)
        self._get_nodes_ranges(args)
        self._get_gpus(args)
        self._get_ports(args)

        self._up_limit_nodes = int(
            os.getenv("PADDLE_EDL_UP_LIMIT_NODES", 1024))

        if self._etcd_endpoints != "" and self._hdfs_home == "":
            self._backend_type = "etcd"
        else:
            self._backend_type = "hdfs"

            # assert hdfs value
            if not self._ce_test:
                assert len(self._hdfs_home) > 3 and \
                    len(self._hdfs_name) > 6 and \
                    len(self._hdfs_ugi) > 3 and \
                    len(self._hdfs_checkpoint_path) > 0, "hdfs environ must set"
            else:
                assert len(self._hdfs_home) > 3 and \
                    len(self._hdfs_checkpoint_path) > 0, "hdfs environ must set"

    @property
    def up_limit_nodes(self):
        return self._up_limit_nodes

    @property
    def gpus(self):
        return self._gpus

    @property
    def nproc_per_node(self):
        return self._nproc_per_node

    @property
    def etcd_endpoints(self):
        return self._etcd_endpoints

    @property
    def job_id(self):
        return self._job_id


class TrainerEnv(object):
    """
    Parse all envs when edl_launch starts a trainer.
    """
    pass
