# Copyright © 2023 Apple Inc.

"""Utilities for executing commands on GCP.

Note that these utilities do not handle resource management.
"""

import atexit
import logging
import os
import pathlib
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Union
from urllib.parse import urlparse

import kubernetes as k8s
from absl import flags
from google.auth.credentials import Credentials

from axlearn.cloud.common.bundler import BaseDockerBundler
from axlearn.cloud.common.job import Job
from axlearn.cloud.common.utils import subprocess_run
from axlearn.cloud.gcp.config import default_project, default_zone, gcp_settings
from axlearn.cloud.gcp.scopes import DEFAULT_TPU_SCOPES
from axlearn.cloud.gcp.system_characteristics import USER_FACING_NAME_TO_SYSTEM_CHARACTERISTICS
from axlearn.cloud.gcp.tpu import (
    get_queued_tpu_node,
    get_tpu_node,
    infer_tpu_type,
    qrm_resource,
    tpu_resource,
)
from axlearn.cloud.gcp.utils import (
    custom_jobset_kwargs,
    delete_k8s_jobset,
    get_credentials,
    running_from_vm,
)
from axlearn.common.config import REQUIRED, ConfigBase, Required, config_class
from axlearn.common.utils import Nested


class GCPJob(Job):
    """Base GCP Job definition."""

    @config_class
    class Config(Job.Config):
        """Configures GCPJob."""

        # GCP project.
        project: Required[str] = REQUIRED
        # GCP zone.
        zone: Required[str] = REQUIRED
        # If not none, the current job will be executed as the service account.
        service_account: Optional[str] = None

    @classmethod
    def define_flags(cls, fv: flags.FlagValues):
        super().define_flags(fv)
        common_kwargs = dict(flag_values=fv, allow_override=True)
        flags.DEFINE_string("project", default_project(), "The GCP project name.", **common_kwargs)
        flags.DEFINE_string("zone", default_zone(), "The GCP zone name.", **common_kwargs)
        flags.DEFINE_string(
            "service_account",
            None,
            "If specified, will run job as the service account. "
            "Otherwise will fallback to application-default credentials.",
            **common_kwargs,
        )

    def _get_job_credentials(
        self,
        impersonate_scopes: Optional[Sequence[str]] = None,
    ) -> Credentials:
        """Returns the credentials the job runs as.

        Note that credentials are temporary and should be created on demand.

        Args:
            impersonate_scopes: Scopes of the impersonation token,
                following https://developers.google.com/identity/protocols/oauth2/scopes

        Returns:
            The temporary credentials, possibly impersonating `cfg.service_account`.
        """
        return get_credentials(
            impersonate_account=self.config.service_account,
            impersonate_scopes=impersonate_scopes,
        )


@config_class
class AcceleratorConfig(ConfigBase):
    """Configures job resources, e.g. TPU or GPU.

    Attributes:
        instance_type: Instance type, e.g. tpu-v4-8.
        num_replicas: Number of replicas, e.g. TPU slices.
    """

    instance_type: Required[str] = REQUIRED
    num_replicas: int = 1


def accelerator_flags(flag_values: flags.FlagValues, **kwargs):
    """Defines resource flags, e.g. --instance_type and --num_replicas."""
    flags.DEFINE_string(
        "instance_type",
        # --instance_type is often defined at the launcher, so use any existing value by default.
        getattr(flag_values, "instance_type", None),
        "Instance type.",
        flag_values=flag_values,
        **kwargs,
    )
    flags.DEFINE_integer(
        "num_replicas", 1, "Number of replicas.", flag_values=flag_values, **kwargs
    )


class TPUQRMJob(GCPJob):
    """Executes arbitrary commands on TPU-VMs."""

    @config_class
    class Config(GCPJob.Config):
        """Configures TPUQRMJob.

        Attributes:
            accelerator: TPU configuration.
        """

        accelerator: AcceleratorConfig = AcceleratorConfig()

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self._local_home = pathlib.Path.home()
        self._use_iap = None  # Infer from public IP.

    @classmethod
    def define_flags(cls, fv: flags.FlagValues):
        super().define_flags(fv)
        accelerator_flags(flag_values=fv, allow_override=True)

    @classmethod
    def from_flags(cls, fv: flags.FlagValues, **kwargs) -> Config:
        cfg: TPUQRMJob.Config = super().from_flags(fv, **kwargs)
        cfg.accelerator.set(instance_type=fv.instance_type, num_replicas=fv.num_replicas)
        return cfg

    def _ensure_ssh_keys(self):
        """Ensures SSH keys exist, or raises ValueError. Only necessary on remote VM."""
        # Seem to need to nuke this every time to avoid MITM warnings.
        hosts_file = self._local_home / ".ssh/google_compute_known_hosts"
        if hosts_file.exists():
            hosts_file.unlink()

        ssh_key = self._local_home / ".ssh/google_compute_engine"
        proc = subprocess_run(f"ssh-add {ssh_key}", check=False, capture_output=True)
        if proc.returncode:
            logging.warning("SSH key %s does not exist yet.", ssh_key)

    def _infer_iap(self):
        """Infers whether instance has public IP. If not, we tunnel through IAP."""
        if self._use_iap is None:
            cfg: TPUQRMJob.Config = self.config
            if cfg.accelerator.num_replicas > 1:
                node = get_queued_tpu_node(
                    cfg.name,
                    qrm_resource(self._get_job_credentials(DEFAULT_TPU_SCOPES)),
                )
            else:
                node = get_tpu_node(
                    cfg.name,
                    tpu_resource(self._get_job_credentials(DEFAULT_TPU_SCOPES)),
                )
            if node is None:
                raise ValueError(f"Expected TPU {cfg.name} to exist")
            for endpoint in node.get("networkEndpoints", []):
                for access_config in endpoint.get("accessConfig", []):
                    if access_config.get("natIP", None):
                        logging.info("Detected a public IP, not using IAP.")
                        self._use_iap = False
                        return False
            logging.info("Didn't find a public IP, using IAP.")
            self._use_iap = True
        return self._use_iap

    def _execute_remote_cmd(
        self,
        cmd: str,
        *,
        worker: Union[int, str] = "all",
        detached_session: Optional[str] = None,
        batch_size: Union[int, str] = 100,
        extra_ssh_flags: str = "",
        **kwargs,
    ) -> Sequence[subprocess.CompletedProcess]:
        """Executes a command on existing TPU-VM(s).

        Args:
            cmd: Command to run.
            worker: Worker ID. Defaults to "all".
            wait: Whether to wait for process to complete. If True, waits for command to complete,
                and returns a completed process. Caller can inspect outputs or exit codes. If False,
                spawns and returns a process. Caller can listen to logs in realtime.
            detached_session: If not None, run commands behind `screen` in detached mode. This is
                useful for persisting commands even if SSH is terminated. If not None, should be a
                string containing the session name.
            batch_size: Number of concurrent command executions. If 'all', run all commands
                simultaneously.
            extra_ssh_flags: Extra gcloud ssh flags.
            **kwargs: Forwarded to subprocess.

        Returns:
            A list of completed subprocesses. Each corresponds to execution of the command on a
            single slice.

        Raises:
            ValueError: If the name of the detached screen session is too long.
        """
        cfg: TPUQRMJob.Config = self.config
        from_vm = running_from_vm()
        cmd = _prepare_cmd_for_gcloud_ssh(f"pushd /root && {cmd}")
        if from_vm:
            self._ensure_ssh_keys()
            extra_ssh_flags = f"--internal-ip {extra_ssh_flags}"
        elif self._infer_iap():
            # Infer IAP flag if not running from VM.
            extra_ssh_flags = f"--tunnel-through-iap {extra_ssh_flags}"
        cmd = f"sudo bash -c {cmd}"
        if detached_session:
            # Even though the official limit is 100 chars, screen seems to silently exit even before
            # that.
            if len(detached_session) > 80:
                raise ValueError(f"Screen name {detached_session} is too long.")
            cmd = f"sudo screen -dmS {detached_session} {cmd}"
        logging.debug("Executing remote command on worker [%s]: '%s'", worker, cmd)
        if cfg.accelerator.num_replicas > 1:
            slices = [f"{cfg.name}-{i}" for i in range(cfg.accelerator.num_replicas)]
        else:
            slices = [cfg.name]
        procs = []
        for s in slices:
            cmd_for_slice = (
                f"gcloud alpha compute -q tpus tpu-vm ssh {s} "
                f"--project={cfg.project} "
                f"--zone={cfg.zone} "
                f"--worker={worker} "
                f"--batch-size={batch_size} "
                f'{extra_ssh_flags} --command="{cmd}"'
            )
            proc = subprocess_run(cmd_for_slice, **_prepare_subprocess_kwargs(kwargs))
            procs.append(proc)
        return procs

    def _execute(self) -> Any:
        """Performs some computation on remote TPU-VMs."""
        cfg: TPUQRMJob.Config = self.config
        self._execute_remote_cmd(cfg.command)

    def execute(self) -> Any:
        """Wraps _execute with ssh-agent and retries. All args and kwargs are forwarded."""
        if running_from_vm():
            _start_ssh_agent()
        return super().execute()


@dataclass
class GCSFuseMount:
    """Configures the GCS FUSE mount.

    https://cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/cloud-storage-fuse-csi-driver#sidecar-container
    https://cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/cloud-storage-fuse-csi-driver#consume-ephemeral-volume-pod

    Attributes:
        gcs_path: GCS path, including gs:// prefix.
        mount_path: Path within local fs to mount to.
        cpu: Defaults to 250m. Increase if higher throughput needed.
        memory: Defaults to 256Mi. Set proportionally to number of files processed (not filesize).
        ephemeral_gb: Defaults to 5Gi. Used for staging temp files before uploading to GCS.
        read_only: Whether the mount should be read-only.
    """

    gcs_path: str
    mount_path: str = "/output"
    cpu: str = "250m"
    memory: str = "256Mi"
    ephemeral_gb: str = "5Gi"
    read_only: bool = False


class GKEJob(GCPJob):
    """Base GKE Job interface."""

    @config_class
    class Config(GCPJob.Config):
        """Configures GKEJob.

        Attributes:
            env_vars: Optional env vars to set.
            namespace: The namespace to use within the k8s cluster.
                https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/
            gcsfuse_mount: Optional configs for the GCS FUSE sidecar and volume mount.
                See `GCSFuseMount` for details.
        """

        env_vars: Dict[str, str] = {}
        namespace: str = "default"
        gcsfuse_mount: Optional[GCSFuseMount] = None

    @classmethod
    def define_flags(cls, fv: flags.FlagValues):
        super().define_flags(fv)
        flags.DEFINE_string(
            "namespace", "default", "K8s namespace.", flag_values=fv, allow_override=True
        )

    @classmethod
    def from_flags(cls, fv: flags.FlagValues, **kwargs) -> Config:
        cfg: GKEJob.Config = super().from_flags(fv, **kwargs)
        cfg.service_account = cfg.service_account or gcp_settings(
            "k8s_service_account", default="default", fv=fv
        )
        return cfg


class TPUGKEJob(GKEJob):
    """A TPU job represented as a k8s JobSet.

    See also `gke_runner` as an example.
    """

    @config_class
    class Config(GKEJob.Config):
        """Configures TPUGKEJob.

        Attributes:
            accelerator: TPU configuration.
            reservation: If specified, the TPU reservation name. This is not necessarily specific to
                GKE and can be the same as e.g. the QRM reservation.
                https://cloud.google.com/sdk/gcloud/reference/alpha/compute/tpus/reservations/list
        """

        accelerator: AcceleratorConfig = AcceleratorConfig()
        reservation: Optional[str] = None

    @classmethod
    def define_flags(cls, fv: flags.FlagValues):
        super().define_flags(fv)
        common_kwargs = dict(flag_values=fv, allow_override=True)
        accelerator_flags(**common_kwargs)
        flags.DEFINE_string("reservation", None, "TPU reservation.", **common_kwargs)

    @classmethod
    def from_flags(cls, fv: flags.FlagValues, **kwargs) -> Config:
        cfg: TPUGKEJob.Config = super().from_flags(fv, **kwargs)
        cfg.accelerator.set(instance_type=fv.instance_type, num_replicas=fv.num_replicas)
        cfg.reservation = cfg.reservation or gcp_settings("gke_reservation", required=False, fv=fv)
        return cfg

    def __init__(self, cfg: Config):
        bundler_cfg = cfg.bundler
        bundler_cfg = getattr(bundler_cfg, "inner", bundler_cfg)
        if bundler_cfg is None or not issubclass(bundler_cfg.klass, BaseDockerBundler):
            raise NotImplementedError(f"Only docker bundler supported, got: {bundler_cfg}")
        self._tpu_type = infer_tpu_type(cfg.accelerator.instance_type)
        if self._tpu_type not in USER_FACING_NAME_TO_SYSTEM_CHARACTERISTICS:
            raise NotImplementedError(f"Missing system characteristics for {self._tpu_type}")
        super().__init__(cfg)
        self._gcsfuse_volume = "gcs-fuse-csi-ephemeral"

    def _build_container(self) -> Nested[Any]:
        """Builds a config for a single container.

        Returns:
            A nested dict corresponding to a k8s Container config.
        """
        cfg: TPUGKEJob.Config = self.config
        system = USER_FACING_NAME_TO_SYSTEM_CHARACTERISTICS[self._tpu_type]
        volume_mounts = []

        if cfg.gcsfuse_mount:
            # https://cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/cloud-storage-fuse-csi-driver#consume-ephemeral-volume-pod
            volume_mounts.append(
                dict(
                    name=self._gcsfuse_volume,
                    mountPath=cfg.gcsfuse_mount.mount_path,
                    readOnly=cfg.gcsfuse_mount.read_only,
                ),
            )

        return dict(
            name=cfg.name,
            image=self._bundler.id(cfg.name),
            # https://cloud.google.com/kubernetes-engine/docs/how-to/tpus#tpu-chips-node-pool
            # https://cloud.google.com/kubernetes-engine/docs/how-to/tpu-multislice#run_workload
            ports=[
                dict(containerPort=8471),  # Port using which TPU VMs communicate.
                dict(containerPort=8080),  # Port for MXLA coordinator.
                dict(containerPort=8431),  # Port to export TPU runtime metrics.
            ],
            securityContext=dict(privileged=True),
            # TODO(markblee): Improve SIGTERM behavior for command.
            command=["bash", "-c", cfg.command],
            resources=dict(limits={"google.com/tpu": system.chips_per_vm}),
            # Env var values should always be strings.
            env=[dict(name=k, value=str(v)) for k, v in cfg.env_vars.items()],
            volumeMounts=volume_mounts,
        )

    def _build_pod(self) -> Nested[Any]:
        """Builds a config for a single Pod, which is a set of containers.

        https://kubernetes.io/docs/concepts/workloads/pods

        Returns:
            A nested dict corresponding to a k8s Pod template, including the pod metadata and spec.
        """
        cfg: TPUGKEJob.Config = self.config
        system = USER_FACING_NAME_TO_SYSTEM_CHARACTERISTICS[self._tpu_type]
        annotations, selector, volumes = {}, {}, []

        if cfg.gcsfuse_mount:
            # Mount a GCS bucket as a volume.
            annotations.update(
                {
                    "gke-gcsfuse/volumes": "true",
                    "gke-gcsfuse/cpu-limit": cfg.gcsfuse_mount.cpu,
                    "gke-gcsfuse/memory-limit": cfg.gcsfuse_mount.memory,
                    "gke-gcsfuse/ephemeral-storage-limit": cfg.gcsfuse_mount.ephemeral_gb,
                }
            )
            # Parse GCSFuseMount path into bucket, prefix.
            parsed = urlparse(cfg.gcsfuse_mount.gcs_path)
            # https://cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/cloud-storage-fuse-csi-driver#consume-ephemeral-volume-pod
            volumes.append(
                dict(
                    name=self._gcsfuse_volume,
                    csi=dict(
                        driver="gcsfuse.csi.storage.gke.io",
                        readOnly=cfg.gcsfuse_mount.read_only,
                        volumeAttributes=dict(
                            bucketName=parsed.netloc,
                            mountOptions=f"only-dir={parsed.path.lstrip('/')}",
                        ),
                    ),
                )
            )

        # If running from bastion, a scheduling tier will be specified in env.
        # Tier "0" corresponds to reserved; otherwise we use preemptible.
        tier = os.environ.get("BASTION_TIER", None)
        if tier == "0" and cfg.reservation:
            logging.info("Found tier=%s in env. Using reservation=%s", tier, cfg.reservation)
            selector.update({"cloud.google.com/reservation-name": cfg.reservation})
        else:
            selector.update({"cloud.google.com/gke-spot": "true"})

        return dict(
            metadata=dict(annotations=annotations),
            spec=dict(
                # NOTE: Don't set hostNetwork or dnsPolicy for compat with Workload Identity.
                terminationGracePeriodSeconds=60,
                # Fail if any pod fails, and allow retries to happen at JobSet level.
                restartPolicy="Never",
                nodeSelector={
                    "cloud.google.com/gke-tpu-accelerator": system.gke_accelerator,
                    "cloud.google.com/gke-tpu-topology": system.topology,
                    # NOTE: This is an arbitrary key, with a value that must be unique to the
                    # jobset. This forces the jobset to be associated with its own node pool;
                    # without this, the TPU provisioner may create a node pool and the scheduler may
                    # schedule a different jobset onto the node pool, which can cause conflicts if
                    # the original jobset attempts to restart (node pool conflict). This is more
                    # reliable at the moment but doesn't take advantage of node pool sharing. GCP is
                    # working on a fix.
                    "provisioner-nodepool-id": cfg.name,
                    **selector,
                },
                containers=[self._build_container()],
                serviceAccountName=cfg.service_account,
                volumes=volumes,
            ),
        )

    def _build_job(self) -> Nested[Any]:
        """Builds a config for a single Job, which is a set of Pods.

        https://kubernetes.io/docs/concepts/workloads/controllers/job/

        Returns:
            A nested dict corresponding to a k8s Job config, including the job metadata and spec.
        """
        system = USER_FACING_NAME_TO_SYSTEM_CHARACTERISTICS[self._tpu_type]
        return dict(
            spec=dict(
                parallelism=system.vms_per_slice,
                completions=system.vms_per_slice,
                backoffLimit=0,  # Fail the job if any node fails. Retries happen at JobSet level.
                template=self._build_pod(),
            ),
        )

    def _build_jobset(self) -> Nested[Any]:
        """Builds a config for a JobSet, which is a set of Jobs.

        https://github.com/kubernetes-sigs/jobset/blob/d49514bee57da8ac9aec2fcea06c3a13c21afeae/docs/concepts/README.md

        Returns:
            A nested dict corresponding to a k8s JobSet config.
        """
        cfg: TPUGKEJob.Config = self.config
        return dict(
            metadata=dict(
                name=cfg.name,
                annotations={
                    # The exlusive topology annotation will ensure that all Pods will have affinity
                    # rules added that will ensure that they are fully scheduled on the same
                    # pod-slice node-pools.
                    "alpha.jobset.sigs.k8s.io/exclusive-topology": "cloud.google.com/gke-nodepool",
                },
            ),
            spec=dict(
                failurePolicy=dict(maxRestarts=cfg.max_tries - 1),
                replicatedJobs=[
                    # NOTE: the suffix here impacts how long job names can be.
                    dict(
                        name="job",
                        replicas=cfg.accelerator.num_replicas,
                        template=self._build_job(),
                    ),
                ],
            ),
        )

    def _delete(self):
        cfg: TPUGKEJob.Config = self.config
        # Issues a delete request for the JobSet and proactively delete its descendants. This is not
        # fully blocking; after the call returns there can be a delay before everything is deleted.
        delete_k8s_jobset(cfg.name, namespace=cfg.namespace)

    def _execute(self) -> Any:
        """Submits a JobSet to the cluster."""
        cfg: TPUGKEJob.Config = self.config
        api_kwargs = custom_jobset_kwargs()
        custom_object = dict(
            apiVersion=f"{api_kwargs['group']}/{api_kwargs['version']}",
            kind="JobSet",
            **self._build_jobset(),
        )
        return k8s.client.CustomObjectsApi().create_namespaced_custom_object(
            namespace=cfg.namespace,
            body=custom_object,
            **api_kwargs,
        )


class CPUJob(GCPJob):
    """Executes arbitrary commands on CPU VMs."""

    def _execute_remote_cmd(
        self, cmd: str, *, detached_session: Optional[str] = None, **kwargs
    ) -> subprocess.CompletedProcess:
        """Executes a command on an existing VM.

        Args:
            cmd: Command to run.
            detached_session: If not None, run commands behind `screen` in detached mode. This is
                useful for persisting commands even if SSH is terminated. If not None, should be a
                string containing the session name.
            **kwargs: Forwarded to subprocess.

        Returns:
            A subprocess, either live or completed.
        """
        cfg: CPUJob.Config = self.config
        logging.debug("Executing remote command: '%s'", cmd)
        cmd = _prepare_cmd_for_gcloud_ssh(f"pushd /root && {cmd}")
        # Use login shell. Note `-i` is not interactive.
        cmd = f"sudo -i bash -c {cmd}"
        if detached_session:
            cmd = f"sudo screen -dmS {detached_session} {cmd}"
        # Run via screen to persist command after SSH.
        cmd = (
            f"gcloud compute -q ssh {cfg.name} "
            f"--project={cfg.project} "
            f"--zone={cfg.zone} "
            f'--command="{cmd}"'
        )
        proc = subprocess_run(cmd, **_prepare_subprocess_kwargs(kwargs))
        logging.debug("Finished launching: '%s'.", cmd)
        return proc

    def _execute(self) -> Any:
        """Performs some computation on remote VMs."""
        cfg: CPUJob.Config = self.config
        self._execute_remote_cmd(cfg.command)


def _prepare_subprocess_kwargs(kwargs: Dict) -> Dict:
    """Enable check=True and capture all outputs by default."""
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", True)
    kwargs.setdefault("capture_output", kwargs.keys().isdisjoint(["stdout", "stderr"]))
    return kwargs


def _kill_ssh_agent():
    """Terminates ssh-agent, e.g. as started by `_start_ssh_agent`."""
    subprocess_run("ssh-agent -k", check=False, capture_output=True)
    os.environ.pop("SSH_AUTH_SOCK", None)
    os.environ.pop("SSH_AGENT_PID", None)


def _start_ssh_agent():
    """Starts ssh-agent for SSH key handling.

    The ssh-agent is automatically terminated when the program exits.
    """
    # pylint: disable=line-too-long
    if not os.getenv("SSH_AGENT_PID"):
        logging.info("ssh-agent is not running, starting it now...")
        process = subprocess_run("ssh-agent -s", stdout=subprocess.PIPE, check=True, text=True)
        # Example format:
        # Linux:
        # SSH_AUTH_SOCK=/tmp/ssh-g4aYlFVLLugX/agent.52090; export SSH_AUTH_SOCK;\nSSH_AGENT_PID=52091; export SSH_AGENT_PID;\necho Agent pid 52091;\n
        # Mac:
        # SSH_AUTH_SOCK=/var/folders/j0/blx8mk5j1hlc0k110xsbrxw00000gn/T//ssh-ZAf5XlQX7tWM/agent.7841; export SSH_AUTH_SOCK;\nSSH_AGENT_PID=7842; export SSH_AGENT_PID;\necho Agent pid 7842;\n
        match = re.search(
            r"SSH_AUTH_SOCK=([^;]+);.*SSH_AGENT_PID=([^;]+);",
            process.stdout,
            re.MULTILINE | re.DOTALL,
        )
        auth_sock, agent_pid = match.groups()  # pytype: disable=attribute-error
        os.environ["SSH_AUTH_SOCK"] = auth_sock
        os.environ["SSH_AGENT_PID"] = agent_pid
        atexit.register(_kill_ssh_agent)
    logging.info("ssh-agent is running.")


def _prepare_cmd_for_gcloud_ssh(cmd: str) -> str:
    """Handles bash escapes to ensure `cmd` is compatible with gcloud `--command`."""
    cmd = shlex.quote(cmd)
    cmd = cmd.replace('"', '\\"')  # Escape double quotes for --command.
    cmd = cmd.replace("$", r"\$")  # Escape $ for --command.
    return cmd


def docker_command(
    cmd: str,
    *,
    image: str,
    detached_session: Optional[str] = None,
    env: Optional[Sequence[str]] = None,
    volumes: Optional[Dict[str, str]] = None,
    extra_docker_flags: Optional[Sequence[str]] = None,
) -> str:
    """Wraps a command with docker run.

    Args:
        cmd: Command to run.
        image: Docker image name.
        detached_session: If not None, runs in detached mode with the given name.
        env: Optional env vars to expose to container.
        volumes: Optional mapping of source/target volumes to mount.
        extra_docker_flags: Optional extra flags for docker run.

    Returns:
        The docker command.
    """
    cmd = _prepare_cmd_for_gcloud_ssh(f"pushd /root && {cmd}")
    cmd = f"/bin/bash -c {cmd}"
    env = " ".join([f"-e {e}" for e in (env or [])])
    volumes = " ".join([f"-v {src}:{dst}" for src, dst in (volumes or {}).items()])
    extra_docker_flags = " ".join(extra_docker_flags or [])
    detached = f"-d --name={detached_session}" if detached_session else ""
    cmd = (
        f"docker run --rm --privileged -u root --network=host {detached} {env} {volumes} "
        f"{extra_docker_flags} {image} {cmd}"
    )
    logging.debug("Docker run command: %s", cmd)
    return cmd
