#
# Copyright (C) 2017 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import cmd
import datetime
import imp  # Python v2 compatibility
import logging
import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import httplib2
from apiclient import errors
import urlparse

from google.protobuf import text_format

from vti.test_serving.proto import TestLabConfigMessage_pb2 as LabCfgMsg
from vti.test_serving.proto import TestScheduleConfigMessage_pb2 as SchedCfgMsg

from host_controller.tfc import request
from host_controller.build import build_flasher
from host_controller.build import build_provider
from host_controller.build import build_provider_ab
from host_controller.build import build_provider_gcs
from host_controller.build import build_provider_local_fs
from host_controller.tradefed import remote_operation
from vts.utils.python.common import cmd_utils

# The default Partner Android Build (PAB) public account.
# To obtain access permission, please reach out to Android partner engineering
# department of Google LLC.
_DEFAULT_ACCOUNT_ID = '543365459'

# The default value for "flash --current".
_DEFAULT_FLASH_IMAGES = [
    build_provider.FULL_ZIPFILE,
    "boot.img",
    "cache.img",
    "system.img",
    "userdata.img",
    "vbmeta.img",
    "vendor.img",
]

# The environment variable for default serial numbers.
_ANDROID_SERIAL = "ANDROID_SERIAL"

DEVICE_STATUS_DICT = {
    "unknown": 0,
    "fastboot": 1,
    "online": 2,
    "ready": 3,
    "use": 4,
    "error": 5}


class ConsoleArgumentError(Exception):
    """Raised when the console fails to parse commands."""
    pass


class ConsoleArgumentParser(argparse.ArgumentParser):
    """The argument parser for a console command."""

    def __init__(self, command_name, description):
        """Initializes the ArgumentParser without --help option.

        Args:
            command_name: A string, the first argument of the command.
            description: The help message about the command.
        """
        super(ConsoleArgumentParser, self).__init__(
            prog=command_name, description=description, add_help=False)

    def ParseLine(self, line):
        """Parses a command line.

        Args:
            line: A string, the command line.

        Returns:
            An argparse.Namespace object.
        """
        return self.parse_args(line.split())

    # @Override
    def error(self, message):
        """Raises an exception when failing to parse the command.

        Args:
            message: The error message.

        Raises:
            ConsoleArgumentError.
        """
        raise ConsoleArgumentError(message)


class Console(cmd.Cmd):
    """The console for host controllers.

    Attributes:
        device_image_info: dict containing info about device image files.
        prompt: The prompt string at the beginning of each command line.
        test_suite_info: dict containing info about test suite package files.
        tools_info: dict containing info about custom tool files.
        build_thread: dict containing threading.Thread instances(s) that
                      update build info regularly.
        scheduler_thread: dict containing threading.Thread instances(s) that
                          update configs regularly.
        update_thread: threading.Thread that updates device state regularly.
        _pab_client: The PartnerAndroidBuildClient used to download artifacts.
        _vti_client: VtiEndpoewrClient, used to upload data to a test
                     scheduling infrastructure.
        _tfc_client: The TfcClient that the host controllers connect to.
        _hosts: A list of HostController objects.
        _in_file: The input file object.
        _out_file: The output file object.
        _serials: A list of string where each string is a device serial.
        _config_parser: The parser for config command.
        _copy_parser: The parser for copy command.
        _device_parser: The parser for device command.
        _fetch_parser: The parser for fetch command.
        _flash_parser: The parser for flash command.
        _gsispl_parser: The parser for gsispl command.
        _info_parser: The parser for info command.
        _lease_parser: The parser for lease command.
        _list_parser: The parser for list command.
        _request_parser: The parser for request command.
    """

    def __init__(self,
                 vti_endpoint_client,
                 tfc,
                 pab,
                 host_controllers,
                 in_file=sys.stdin,
                 out_file=sys.stdout):
        """Initializes the attributes and the parsers."""
        # cmd.Cmd is old-style class.
        cmd.Cmd.__init__(self, stdin=in_file, stdout=out_file)
        self._build_provider = {}
        self._build_provider["pab"] = pab
        self._build_provider[
            "local_fs"] = build_provider_local_fs.BuildProviderLocalFS()
        self._build_provider["gcs"] = build_provider_gcs.BuildProviderGCS()
        self._build_provider["ab"] = build_provider_ab.BuildProviderAB()
        self._vti_endpoint_client = vti_endpoint_client
        self._tfc_client = tfc
        self._hosts = host_controllers
        self._in_file = in_file
        self._out_file = out_file
        self.prompt = "> "
        self.device_image_info = {}
        self.test_suite_info = {}
        self.tools_info = {}
        self.build_thread = {}
        self.schedule_thread = {}
        self.update_thread = None
        self.fetch_info = {}

        self._InitBuildParser()
        self._InitCopyParser()
        self._InitDeviceParser()
        self._InitFetchParser()
        self._InitFlashParser()
        self._InitGsiSplParser()
        self._InitInfoParser()
        self._InitLeaseParser()
        self._InitListParser()
        self._InitRequestParser()
        self._InitConfigParser()
        self._InitTestParser()

    def _InitRequestParser(self):
        """Initializes the parser for request command."""
        self._request_parser = ConsoleArgumentParser(
            "request", "Send TFC a request to execute a command.")
        self._request_parser.add_argument(
            "--cluster",
            required=True,
            help="The cluster to which the request is submitted.")
        self._request_parser.add_argument(
            "--run-target",
            required=True,
            help="The target device to run the command.")
        self._request_parser.add_argument(
            "--user",
            required=True,
            help="The name of the user submitting the request.")
        self._request_parser.add_argument(
            "command",
            metavar="COMMAND",
            nargs="+",
            help='The command to be executed. If the command contains '
            'arguments starting with "-", place the command after '
            '"--" at end of line.')

    def ProcessScript(self, script_file_path):
        """Processes a .py script file.

        A script file implements a function which emits a list of console
        commands to execute. That function emits an empty list or None if
        no more command needs to be processed.

        Args:
            script_file_path: string, the path of a script file (.py file).

        Returns:
            True if successful; False otherwise
        """
        if not script_file_path.endswith(".py"):
            print("Script file is not .py file: %s" % script_file_path)
            return False

        script_module = imp.load_source('script_module', script_file_path)

        if _ANDROID_SERIAL in os.environ:
            serial = os.environ[_ANDROID_SERIAL]
        else:
            serial = None

        if serial:
            self.onecmd("device --set_serial=%s" % serial)

        commands = script_module.EmitConsoleCommands()
        if commands:
            for command in commands:
                self.onecmd(command)
        return True

    def do_request(self, line):
        """Sends TFC a request to execute a command."""
        args = self._request_parser.ParseLine(line)
        req = request.Request(
            cluster=args.cluster,
            command_line=" ".join(args.command),
            run_target=args.run_target,
            user=args.user)
        self._tfc_client.NewRequest(req)

    def help_request(self):
        """Prints help message for request command."""
        self._request_parser.print_help(self._out_file)

    def _InitListParser(self):
        """Initializes the parser for list command."""
        self._list_parser = ConsoleArgumentParser(
            "list", "Show information about the hosts.")
        self._list_parser.add_argument(
            "--host", type=int, help="The index of the host.")
        self._list_parser.add_argument(
            "type",
            choices=("hosts", "devices"),
            help="The type of the shown objects.")

    def _Print(self, string):
        """Prints a string and a new line character.

        Args:
            string: The string to be printed.
        """
        self._out_file.write(string + "\n")

    def do_list(self, line):
        """Shows information about the hosts."""
        args = self._list_parser.ParseLine(line)
        if args.host is None:
            hosts = enumerate(self._hosts)
        else:
            hosts = [(args.host, self._hosts[args.host])]
        if args.type == "hosts":
            self._PrintHosts(self._hosts)
        elif args.type == "devices":
            for ind, host in hosts:
                devices = host.ListDevices()
                self._Print("[%3d]  %s" % (ind, host.hostname))
                self._PrintDevices(devices)

    def help_list(self):
        """Prints help message for list command."""
        self._list_parser.print_help(self._out_file)

    def _PrintHosts(self, hosts):
        """Shows a list of host controllers.

        Args:
            hosts: A list of HostController objects.
        """
        self._Print("index  name")
        for ind, host in enumerate(hosts):
            self._Print("[%3d]  %s" % (ind, host.hostname))

    def _PrintDevices(self, devices):
        """Shows a list of devices.

        Args:
            devices: A list of DeviceInfo objects.
        """
        attr_names = ("device_serial", "state", "run_target", "build_id",
                      "sdk_version", "stub")
        self._PrintObjects(devices, attr_names)

    def _PrintObjects(self, objects, attr_names):
        """Shows objects as a table.

        Args:
            object: The objects to be shown, one object in a row.
            attr_names: The attributes to be shown, one attribute in a column.
        """
        width = [len(name) for name in attr_names]
        rows = [attr_names]
        for dev_info in objects:
            attrs = [
                _ToPrintString(getattr(dev_info, name, ""))
                for name in attr_names
            ]
            rows.append(attrs)
            for index, attr in enumerate(attrs):
                width[index] = max(width[index], len(attr))

        for row in rows:
            self._Print("  ".join(
                attr.ljust(width[index]) for index, attr in enumerate(row)))

    def _InitLeaseParser(self):
        """Initializes the parser for lease command."""
        self._lease_parser = ConsoleArgumentParser(
            "lease", "Make a host lease command tasks from TFC.")
        self._lease_parser.add_argument(
            "--host", type=int, help="The index of the host.")

    def do_lease(self, line):
        """Makes a host lease command tasks from TFC."""
        args = self._lease_parser.ParseLine(line)
        if args.host is None:
            if len(self._hosts) > 1:
                raise ConsoleArgumentError("More than one hosts.")
            args.host = 0
        tasks = self._hosts[args.host].LeaseCommandTasks()
        self._PrintTasks(tasks)

    def help_lease(self):
        """Prints help message for lease command."""
        self._lease_parser.print_help(self._out_file)

    def _InitFetchParser(self):
        """Initializes the parser for fetch command."""
        self._fetch_parser = ConsoleArgumentParser("fetch",
                                                   "Fetch a build artifact.")
        self._fetch_parser.add_argument(
            '--type',
            default='pab',
            choices=('local_fs', 'gcs', 'pab', 'ab'),
            help='Build provider type')
        self._fetch_parser.add_argument(
            '--method',
            default='GET',
            choices=('GET', 'POST'),
            help='Method for fetching')
        self._fetch_parser.add_argument(
            "--path",  # required for local_fs
            help="The path of a local directory which keeps the artifacts.")
        self._fetch_parser.add_argument(
            "--branch",  # required for pab
            help="Branch to grab the artifact from.")
        self._fetch_parser.add_argument(
            "--target",  # required for pab
            help="Target product to grab the artifact from.")
        # TODO(lejonathan): find a way to not specify this?
        self._fetch_parser.add_argument(
            "--account_id",
            default=_DEFAULT_ACCOUNT_ID,
            help="Partner Android Build account_id to use.")
        self._fetch_parser.add_argument(
            '--build_id',
            default='latest',
            help='Build ID to use default latest.')
        self._fetch_parser.add_argument(
            "--artifact_name",  # required for pab
            help=
            "Name of the artifact to be fetched. {id} replaced with build id.")
        self._fetch_parser.add_argument(
            "--userinfo_file",
            help=
            "Location of file containing email and password, if using POST.")
        self._fetch_parser.add_argument(
            "--tool", help="The path of custom tool to be fetched from GCS.")

    def do_fetch(self, line):
        """Makes the host download a build artifact from PAB."""
        args = self._fetch_parser.ParseLine(line)
        if args.type == "pab":
            # do we want this somewhere else? No harm in doing multiple times
            self._build_provider[args.type].Authenticate(args.userinfo_file)
            device_images, test_suites, fetch_environment = self._build_provider[
                args.type].GetArtifact(
                    account_id=args.account_id,
                    branch=args.branch,
                    target=args.target,
                    artifact_name=args.artifact_name,
                    build_id=args.build_id,
                    method=args.method)
            self.fetch_info["build_id"] = fetch_environment["build_id"]
        elif args.type == "local_fs":
            device_images, test_suites = self._build_provider[args.type].Fetch(
                args.path)
            self.fetch_info["build_id"] = None
        elif args.type == "gcs":
            device_images, test_suites, tools = self._build_provider[
                args.type].Fetch(args.path, args.tool)
            self.tools_info.update(tools)
            self.fetch_info["build_id"] = None
        elif args.type == "ab":
            device_images, test_suites, fetch_environment = self._build_provider[
                args.type].Fetch(
                    branch=args.branch,
                    target=args.target,
                    artifact_name=args.artifact_name,
                    build_id=args.build_id)
            self.fetch_info["build_id"] = fetch_environment["build_id"]
        else:
            print("ERROR: unknown fetch type %s" % args.type)
            return

        self.fetch_info["branch"] = args.branch
        self.fetch_info["target"] = args.target

        self.device_image_info.update(device_images)
        self.test_suite_info.update(test_suites)

        if self.device_image_info:
            logging.info("device images:\n%s", "\n".join(
                image + ": " + path
                for image, path in self.device_image_info.iteritems()))
        if self.test_suite_info:
            logging.info("test suites:\n%s", "\n".join(
                suite + ": " + path
                for suite, path in self.test_suite_info.iteritems()))

    def help_fetch(self):
        """Prints help message for fetch command."""
        self._fetch_parser.print_help(self._out_file)

    def DownloadTestResources(self, request_id):
        """Download all of the test resources for a TFC request id.

        Args:
            request_id: int, TFC request id
        """
        resources = self._tfc_client.TestResourceList(request_id)
        for resource in resources:
            self.DownloadTestResource(resource['url'])

    def DownloadTestResource(self, url):
        """Download a test resource with build provider, given a url.

        Args:
            url: a resource locator (not necessarily HTTP[s])
                with the scheme specifying the build provider.
        """
        parsed = urlparse.urlparse(url)
        path = (parsed.netloc + parsed.path).split('/')
        # pab://5346564/oc-release/marlin-userdebug/4329875/artifact.img
        if parsed.scheme == "pab":
            if len(path) != 5:
                print("Invalid pab resource locator: %s" % url)
                return
            account_id, branch, target, build_id, artifact_name = path
            cmd = ("fetch"
                   " --type=pab"
                   " --account_id=%s"
                   " --branch=%s"
                   " --target=%s"
                   " --build_id=%s"
                   " --artifact_name=%s") % (account_id, branch, target,
                                             build_id, artifact_name)
            self.onecmd(cmd)
        # ab://oc-release/marlin-userdebug/4329875/artifact.img
        elif parsed.scheme == "ab":
            if len(path) != 4:
                print("Invalid ab resource locator: %s" % url)
                return
            branch, target, build_id, artifact_name = path
            cmd = ("fetch"
                   "--type=ab"
                   " --branch=%s"
                   " --target=%s"
                   " --build_id=%s"
                   " --artifact_name=%s") % (branch, target, build_id,
                                             artifact_name)
            self.onecmd(cmd)
        elif parsed.scheme == gcs:
            cmd = "fetch --type=gcs --path=%s" % url
            self.onecmd(cmd)
        else:
            print "Invalid URL: %s" % url

    def _InitFlashParser(self):
        """Initializes the parser for flash command."""
        self._flash_parser = ConsoleArgumentParser("flash",
                                                   "Flash images to a device.")
        self._flash_parser.add_argument(
            "--current",
            metavar="PARTITION_IMAGE",
            nargs="*",
            type=lambda x: x.split("="),
            help="The partitions and images to be flashed. The format is "
            "<partition>=<image>. If PARTITION_IMAGE list is empty, "
            "currently fetched " + ", ".join(_DEFAULT_FLASH_IMAGES) +
            " will be flashed.")
        self._flash_parser.add_argument(
            "--serial", default="", help="Serial number for device.")
        self._flash_parser.add_argument(
            "--build_dir",
            help="Directory containing build images to be flashed.")
        self._flash_parser.add_argument(
            "--gsi", help="Path to generic system image")
        self._flash_parser.add_argument(
            "--vbmeta", help="Path to vbmeta image")
        self._flash_parser.add_argument(
            "--flasher_type",
            default="fastboot",
            choices=("fastboot", "custom"),
            help="Flasher binary type")
        self._flash_parser.add_argument(
            "--flasher_path", help="Path to flasher binary")
        self._flash_parser.add_argument(
            "--reboot_mode",
            default="bootloader",
            choices=("bootloader", "download"),
            help="Reboot device to bootloader/download mode")
        self._flash_parser.add_argument(
            "--arg_flasher", help="Argument passed to the custom binary")
        self._flash_parser.add_argument(
            "--repackage",
            default="tar.md5",
            choices=("tar.md5"),
            help="Repackage artifacts into given format before flashing.")

    def do_flash(self, line):
        """Flash GSI or build images to a device connected with ADB."""
        args = self._flash_parser.ParseLine(line)

        if self.tools_info is not None:
            flasher_path = self.tools_info[args.flasher_path]
        else:
            flasher_path = args.flasher_path

        flashers = []
        if args.serial:
            flasher = build_flasher.BuildFlasher(args.serial, flasher_path)
            flashers.append(flasher)
        elif self._serials:
            for serial in self._serials:
                flasher = build_flasher.BuildFlasher(serial, flasher_path)
                flashers.append(flasher)
        else:
            flasher = build_flasher.BuildFlasher("", flasher_path)
            flashers.append(flasher)

        if args.current:
            partition_image = dict((partition, self.device_image_info[image])
                                   for partition, image in args.current)
        else:
            partition_image = dict((image.rsplit(".img", 1)[0],
                                    self.device_image_info[image])
                                   for image in _DEFAULT_FLASH_IMAGES
                                   if image in self.device_image_info)

        if flashers:
            # Can be parallelized as long as that's proven reliable.
            for flasher in flashers:
                if args.current is not None:
                    flasher.Flash(partition_image)
                else:
                    if args.flasher_type is None or args.flasher_type == "fastboot":
                        if args.gsi is None and args.build_dir is None:
                            self._flash_parser.error(
                                "Nothing requested: specify --gsi or --build_dir"
                            )
                        if args.build_dir is not None:
                            flasher.Flashall(args.build_dir)
                        if args.gsi is not None:
                            flasher.FlashGSI(args.gsi, args.vbmeta)
                    elif args.flasher_type == "custom":
                        if flasher_path is not None:
                            if args.repackage is not None:
                                flasher.RepackageArtifacts(
                                    self.device_image_info, args.repackage)
                            flasher.FlashUsingCustomBinary(
                                self.device_image_info, args.reboot_mode,
                                args.arg_flasher, 300)
                        else:
                            self._flash_parser.error(
                                "Please specify the path to custom flash tool."
                            )
                    else:
                        self._flash_parser.error(
                            "Wrong flasher type requested: --flasher_type=%s" %
                            args.flasher_type)

            for flasher in flashers:
                flasher.WaitForDevice()

    def help_flash(self):
        """Prints help message for flash command."""
        self._flash_parser.print_help(self._out_file)

    def _InitBuildParser(self):
        """Initializes the parser for build command."""
        self._build_parser = ConsoleArgumentParser(
            "build", "Specifies branches and targets to monitor.")
        self._build_parser.add_argument(
            "--update",
            choices=("single", "start", "stop", "list"),
            default="start",
            help="Update build info")
        self._build_parser.add_argument(
            "--id",
            default=None,
            help="session ID only required for 'stop' update command")
        self._build_parser.add_argument(
            "--interval",
            type=int,
            default=30,
            help="Interval (seconds) to repeat build update.")
        self._build_parser.add_argument(
            "--artifact-type",
            choices=("device", "gsi", "test"),
            default="device",
            help="The type of an artifact to update")
        self._build_parser.add_argument(
            "--branch",
            required=True,
            help="Branch to grab the artifact from.")
        self._build_parser.add_argument(
            "--target",
            required=True,
            help="a comma-separate list of build target product(s).")
        self._build_parser.add_argument(
            "--account_id",
            default=_DEFAULT_ACCOUNT_ID,
            help="Partner Android Build account_id to use.")

    def UpdateBuild(self, account_id, branch, targets, artifact_type):
        """Updates the build state.

        Args:
            account_id: string, Partner Android Build account_id to use.
            branch: string, branch to grab the artifact from.
            targets: string, a comma-separate list of build target product(s).
            artifact_type: string, artifcat type (`device`, 'gsi' or `test').
        """
        builds = []

        self._build_provider["pab"].Authenticate()
        for target in targets.split(","):
            listed_builds = self._build_provider[
                "pab"].GetBuildList(
                    account_id=account_id,
                    branch=branch,
                    target=target,
                    page_token="",
                    max_results=100,
                    method="GET")

            for listed_build in listed_builds:
                if listed_build["successful"]:
                    build = {}
                    build["manifest_branch"] = branch
                    build["build_id"] = listed_build["build_id"]
                    if "-" in target:
                        build["build_target"], build["build_type"] = target.split("-")
                    else:
                        build["build_target"] = target
                        build["build_type"] = ""
                    build["artifact_type"] = artifact_type
                    build["artifacts"] = []
                    builds.append(build)
        self._vti_endpoint_client.UploadBuildInfo(builds)

    def UpdateBuildLoop(self, account_id, branch, target, artifact_type,
                        update_interval):
        """Regularly updates the build information.

        Args:
            account_id: string, Partner Android Build account_id to use.
            branch: string, branch to grab the artifact from.
            targets: string, a comma-separate list of build target product(s).
            artifact_type: string, artifcat type (`device`, 'gsi' or `test).
            update_interval: int, number of seconds before repeating
        """
        thread = threading.currentThread()
        while getattr(thread, 'keep_running', True):
            try:
                self.UpdateBuild(account_id, branch, target, artifact_type)
            except (socket.error, remote_operation.RemoteOperationException,
                    httplib2.HttpLib2Error, errors.HttpError) as e:
                logging.exception(e)
            time.sleep(update_interval)

    def do_build(self, line):
        """Updates build info."""
        args = self._build_parser.ParseLine(line)
        if args.update == "single":
            self.UpdateDevice(
                args.account_id,
                args.branch,
                args.target,
                args.artifact_type)
        elif args.update == "list":
            print("Running build update sessions:")
            for id in self.build_thread:
                print("  ID %d", id)
        elif args.update == "start":
            if args.interval <= 0:
                raise ConsoleArgumentError(
                    "update interval must be positive")
            # do not allow user to create new
            # thread if one is currently running
            if args.id is None:
                if not self.build_thread:
                  args.id = 1
                else:
                  args.id = max(self.build_thread) + 1
            else:
                args.id = int(args.id)
            if args.id in self.build_thread and not hasattr(
                    self.build_thread[args.id], 'keep_running'):
                print(
                    'build update (session ID: %s) already running. '
                    'run build --update stop first.' % args.id
                )
                return
            self.build_thread[args.id] = threading.Thread(
                target=self.UpdateBuildLoop,
                args=(
                    args.account_id,
                    args.branch,
                    args.target,
                    args.artifact_type,
                    args.interval, ))
            self.build_thread[args.id].daemon = True
            self.build_thread[args.id].start()
        elif args.update == "stop":
            if args.id is None:
                print("--id must be set for stop")
            else:
                self.build_thread[int(args.id)].keep_running = False

    def help_build(self):
        """Prints help message for build command."""
        self._build_parser.print_help(self._out_file)
        print("Sample: build --target=aosp_sailfish-userdebug "
              "--branch=<branch name> --artifact-type=device")

    def _InitConfigParser(self):
        """Initializes the parser for config command."""
        self._config_parser = ConsoleArgumentParser(
            "config", "Specifies a global config type to monitor.")
        self._config_parser.add_argument(
            "--update",
            choices=("single", "start", "stop", "list"),
            default="start",
            help="Update build info")
        self._config_parser.add_argument(
            "--id",
            default=None,
            help="session ID only required for 'stop' update command")
        self._config_parser.add_argument(
            "--interval",
            type=int,
            default=60,
            help="Interval (seconds) to repeat build update.")
        self._config_parser.add_argument(
            "--config-type",
            choices=("prod", "test"),
            default="prod",
            help="Whether it's for prod")
        self._config_parser.add_argument(
            "--branch",
            required=True,
            help="Branch to grab the artifact from.")
        self._config_parser.add_argument(
            "--target",
            required=True,
            help="a comma-separate list of build target product(s).")
        self._config_parser.add_argument(
            "--account_id",
            default=_DEFAULT_ACCOUNT_ID,
            help="Partner Android Build account_id to use.")
        self._config_parser.add_argument(
            '--method',
            default='GET',
            choices=('GET', 'POST'),
            help='Method for fetching')

    def UpdateConfig(self, account_id, branch, targets, config_type, method):
        """Updates the global configuration data.

        Args:
            account_id: string, Partner Android Build account_id to use.
            branch: string, branch to grab the artifact from.
            targets: string, a comma-separate list of build target product(s).
            config_type: string, config type (`prod` or `test').
            method: string, HTTP method for fetching.
        """

        self._build_provider["pab"].Authenticate()
        for target in targets.split(","):
            listed_builds = self._build_provider[
                "pab"].GetBuildList(
                    account_id=account_id,
                    branch=branch,
                    target=target,
                    page_token="",
                    max_results=1,
                    method="GET")

            if listed_builds and len(listed_builds) > 0:
                listed_build = listed_builds[0]
                if listed_build["successful"]:
                    device_images, test_suites, artifacts, configs = self._build_provider[
                        "pab"].GetArtifact(
                            account_id=account_id,
                            branch=branch,
                            target=target,
                            artifact_name=("vti-global-config-%s.zip" % config_type),
                            build_id=listed_build["build_id"],
                            method=method)
                    base_path = os.path.dirname(configs[config_type])
                    schedules_pbs = []
                    lab_pbs = []
                    for root, dirs, files in os.walk(base_path):
                        for config_file in files:
                            full_path = os.path.join(root, config_file)
                            if file.endswith(".schedule_config"):
                                with open(full_path, "r") as fd:
                                  context = fd.read()
                                  sched_cfg_msg = SchedCfgMsg.ScheduleConfigMessage()
                                  text_format.Merge(context, sched_cfg_msg)
                                  schedules_pbs.append(sched_cfg_msg)
                                  print sched_cfg_msg.manifest_branch
                            elif config_file.endswith(".lab_config"):
                                with open(full_path, "r") as fd:
                                  context = fd.read()
                                  lab_cfg_msg = LabCfgMsg.LabConfigMessage()
                                  text_format.Merge(context, lab_cfg_msg)
                                  lab_pbs.append(lab_cfg_msg)
                    self._vti_endpoint_client.UploadScheduleInfo(schedules_pbs)
                    self._vti_endpoint_client.UploadLabInfo(lab_pbs)

    def UpdateConfigLoop(self, account_id, branch, target, config_type, method, update_interval):
        """Regularly updates the global configuration.

        Args:
            account_id: string, Partner Android Build account_id to use.
            branch: string, branch to grab the artifact from.
            targets: string, a comma-separate list of build target product(s).
            config_type: string, config type (`prod` or `test').
            method: string, HTTP method for fetching.
            update_interval: int, number of seconds before repeating
        """
        thread = threading.currentThread()
        while getattr(thread, 'keep_running', True):
            try:
                self.UpdateConfig(account_id, branch, target, config_type, method)
            except (socket.error, remote_operation.RemoteOperationException,
                    httplib2.HttpLib2Error, errors.HttpError) as e:
                logging.exception(e)
            time.sleep(update_interval)

    def do_config(self, line):
        """Updates global config."""
        args = self._config_parser.ParseLine(line)
        if args.update == "single":
            self.UpdateConfig(
                args.account_id,
                args.branch,
                args.target,
                args.config_type,
                args.method)
        elif args.update == "list":
            print("Running config update sessions:")
            for id in self.schedule_thread:
                print("  ID %d", id)
        elif args.update == "start":
            if args.interval <= 0:
                raise ConsoleArgumentError(
                    "update interval must be positive")
            # do not allow user to create new
            # thread if one is currently running
            if args.id is None:
                if not self.schedule_thread:
                  args.id = 1
                else:
                  args.id = max(self.schedule_thread) + 1
            else:
                args.id = int(args.id)
            if args.id in self.schedule_thread and not hasattr(
                    self.schedule_thread[args.id], 'keep_running'):
                print(
                    'config update already running. '
                    'run config --update=stop --id=%s first.' % args.id
                )
                return
            self.schedule_thread[args.id] = threading.Thread(
                target=self.UpdateConfigLoop,
                args=(
                    args.account_id,
                    args.branch,
                    args.target,
                    args.config_type,
                    args.method,
                    args.interval, ))
            self.schedule_thread[args.id].daemon = True
            self.schedule_thread[args.id].start()
        elif args.update == "stop":
            if args.id is None:
                print("--id must be set for stop")
            else:
                self.schedule_thread[int(args.id)].keep_running = False

    def help_config(self):
        """Prints help message for config command."""
        self._config_parser.print_help(self._out_file)
        print("Sample: schedule --target=aosp_sailfish-userdebug "
              "--branch=git_oc-release")

    def _InitCopyParser(self):
        """Initializes the parser for copy command."""
        self._copy_parser = ConsoleArgumentParser("copy", "Copy a file.")

    def do_copy(self, line):
        """Copy a file from source to destination path."""
        src, dst = line.split()
        if dst == "{vts_tf_home}":
            dst = os.path.dirname(self.test_suite_info["vts"])
        elif "{" in dst:
            print("unknown dst %s" % dst)
            return
        shutil.copy(src, dst)

    def help_copy(self):
        """Prints help message for copy command."""
        self._copy_parser.print_help(self._out_file)

    def _InitDeviceParser(self):
        """Initializes the parser for device command."""
        self._device_parser = ConsoleArgumentParser(
            "device", "Selects device(s) under test.")
        self._device_parser.add_argument(
            "--set_serial",
            default="",
            help="Serial number for device. Can be a comma-separated list.")
        self._device_parser.add_argument(
            "--update",
            choices=("single", "start", "stop"),
            default="start",
            help="Update device info on cloud scheduler")
        self._device_parser.add_argument(
            "--interval",
            type=int,
            default=30,
            help="Interval (seconds) to repeat device update.")
        self._device_parser.add_argument(
            "--host", type=int, help="The index of the host.")
        self._device_parser.add_argument(
            "--server_type",
            choices=("vti", "tfc"),
            default="vti",
            help="The type of a cloud-based test scheduler server.")
        self._serials = []

    def UpdateDevice(self, server_type, host):
        """Updates the device state of all devices on a given host.

        Args:
            server_type: string, the type of a test secheduling server.
            host: HostController object
        """
        if server_type == "vti":
            devices = []

            stdout, stderr, returncode = cmd_utils.ExecuteOneShellCommand(
                "adb devices")

            lines = stdout.split("\n")[1:]
            for line in lines:
                if len(line.strip()):
                    device = {}
                    device["serial"] = line.split()[0]
                    stdout, _, retcode = cmd_utils.ExecuteOneShellCommand(
                        "adb -s %s shell getprop ro.product.board" % device["serial"])
                    if retcode == 0:
                        device["product"] = stdout.strip()
                    else:
                        device["product"] = "error"
                    device["status"] = DEVICE_STATUS_DICT["online"]
                    devices.append(device)

            stdout, stderr, returncode = cmd_utils.ExecuteOneShellCommand(
                "fastboot devices")
            lines = stdout.split("\n")
            for line in lines:
                if len(line.strip()):
                    device = {}
                    device["serial"] = line.split()[0]
                    device["product"] = "unknown"
                    device["status"] = DEVICE_STATUS_DICT["fastboot"]
                    devices.append(device)

            self._vti_endpoint_client.UploadDeviceInfo(
                host.hostname, devices)
        elif server_type == "tfc":
            devices = host.ListDevices()
            for device in devices:
                device.Extend(['sim_state', 'sim_operator', 'mac_address'])
            snapshots = self._tfc_client.CreateDeviceSnapshot(
                host._cluster_ids[0], host.hostname, devices)
            self._tfc_client.SubmitHostEvents([snapshots])
        else:
            print "Error: unknown server_type %s for UpdateDevice" % server_type

    def UpdateDeviceRepeat(self, server_type, host, update_interval):
        """Regularly updates the device state of devices on a given host.

        Args:
            server_type: string, the type of a test secheduling server.
            host: HostController object
            update_interval: int, number of seconds before repeating
        """
        thread = threading.currentThread()
        while getattr(thread, 'keep_running', True):
            try:
                self.UpdateDevice(server_type, host)
            except (socket.error, remote_operation.RemoteOperationException,
                    httplib2.HttpLib2Error, errors.HttpError) as e:
                logging.exception(e)
            time.sleep(update_interval)

    def do_device(self, line):
        """Sets device info such as serial number."""
        args = self._device_parser.ParseLine(line)
        if args.set_serial:
            self._serials = args.set_serial.split(",")
            print("serials: %s" % self._serials)
        if args.update:
            if args.host is None:
                if len(self._hosts) > 1:
                    raise ConsoleArgumentError("More than one host.")
                args.host = 0
            host = self._hosts[args.host]
            if args.update == "single":
                self.UpdateDevice(args.server_type, host)
            elif args.update == "start":
                if args.interval <= 0:
                    raise ConsoleArgumentError(
                        "update interval must be positive")
                # do not allow user to create new
                # thread if one is currently running
                if self.update_thread is not None and not hasattr(
                        self.update_thread, 'keep_running'):
                    print('device update already running. '
                          'run device --update stop first.')
                    return
                self.update_thread = threading.Thread(
                    target=self.UpdateDeviceRepeat,
                    args=(
                        args.server_type,
                        host,
                        args.interval,
                    ))
                self.update_thread.daemon = True
                self.update_thread.start()
            elif args.update == "stop":
                self.update_thread.keep_running = False

    def help_device(self):
        """Prints help message for device command."""
        self._device_parser.print_help(self._out_file)

    def _InitGsiSplParser(self):
        """Initializes the parser for device command."""
        self._gsisplParser = ConsoleArgumentParser(
            "gsispl", "Changes security patch level on a selected GSI file.")
        self._gsisplParser.add_argument(
            "--gsi",
            help="Path to GSI image to change security patch level. "
            "If path is not given, the most recently fetched system.img "
            "kept in device_image_info dictionary is used and then "
            "device_image_info will be updated with the new GSI file.")
        self._gsisplParser.add_argument(
            "--version",
            required=True,
            help="New version ID. It should be YYYY-mm-dd format.")

    def do_gsispl(self, line):
        """Changes security patch level on a selected GSI file."""
        args = self._gsisplParser.ParseLine(line)
        if args.gsi:
            if os.path.isfile(args.gsi):
                gsi_path = args.gsi
            else:
                print "Cannot find system image in given path"
                return
        elif "system.img" in self.device_image_info:
            gsi_path = self.device_image_info["system.img"]
        else:
            print "Cannot find system image."
            return

        if args.version:
            try:
                version_date = datetime.datetime.strptime(
                    args.version, "%Y-%m-%d")
                version = "{:04d}-{:02d}-{:02d}".format(
                    version_date.year, version_date.month, version_date.day)
            except ValueError as e:
                print "version ID should be YYYY-mm-dd format."
                return
        else:
            print "version ID must be given."
            return

        output_path = os.path.join(os.path.dirname(os.path.abspath(gsi_path)),
            "system-{}.img".format(version))
        stdout, stderr, err_code = cmd_utils.ExecuteOneShellCommand(
            "{} {} {} {}".format(
                "vts/harnesses/host_controller/gsi/change_spl.sh", gsi_path,
                output_path, version))
        if err_code is 0:
            if not args.gsi:
                self.device_image_info["system.img"] = output_path
        else:
            print "gsispl error: {}".format(stderr)
            return

    def help_gsispl(self):
        """Prints help message for gsispl command."""
        self._gsisplParser.print_help(self._out_file)

    def _InitTestParser(self):
        """Initializes the parser for test command."""
        self._test_parser = ConsoleArgumentParser("test",
                                                  "Executes a command on TF.")
        self._test_parser.add_argument(
            "--serial",
            default=None,
            help=("The target device serial to run the command. "
                  "A comma-separate list."))
        self._test_parser.add_argument(
            "--test_exec_mode",
            default="subprocess",
            help="The target exec model.")
        self._test_parser.add_argument(
            "command",
            metavar="COMMAND",
            nargs="+",
            help='The command to be executed. If the command contains '
            'arguments starting with "-", place the command after '
            '"--" at end of line. format: plan -m module -t testcase')

    def do_test(self, line):
        """Executes a command using a VTS-TF instance."""
        args = self._test_parser.ParseLine(line)
        if args.serial:
            serials = args.serial.split(",")
        elif self._serials:
            serials = self._serials
        else:
            serials = []

        if args.test_exec_mode == "subprocess":
            if "vts" not in self.test_suite_info:
                 print("test_suite_info doesn't have 'vts': %s" %
                       self.test_suite_info)
            else:
                bin_path = self.test_suite_info["vts"]
                cmd = [bin_path, "run"]
                cmd.extend(args.command)
                if serials:
                    for serial in serials:
                        cmd.extend(["-s", str(serial)])
                print("Command: %s" % cmd)
                result = subprocess.check_output(cmd)
                logging.debug("result: %s", result)
        else:
            print("unsupported exec mode: %s", args.test_exec_mode)

    def help_test(self):
        """Prints help message for test command."""
        self._test_parser.print_help(self._out_file)

    def _InitInfoParser(self):
        """Initializes the parser for info command."""
        self._info_parser = ConsoleArgumentParser("info",
                                                  "Show status.")

    def do_info(self, line):
        """Shows the console's session status information."""
        print("device image: %s" % self.device_image_info)
        print("test suite: %s" % self.test_suite_info)
        print("fetch info: %s" % self.fetch_info)

    def help_info(self):
        """Prints help message for info command."""
        self._info_parser.print_help(self._out_file)

    def _PrintTasks(self, tasks):
        """Shows a list of command tasks.

        Args:
            devices: A list of DeviceInfo objects.
        """
        attr_names = ("request_id", "command_id", "task_id", "device_serials",
                      "command_line")
        self._PrintObjects(tasks, attr_names)

    def do_exit(self, line):
        """Terminates the console.

        Returns:
            True, which stops the cmdloop.
        """
        return True

    def help_exit(self):
        """Prints help message for exit command."""
        self._Print("Terminate the console.")

    # @Override
    def onecmd(self, line):
        """Executes command(s) and prints any exception.

        Args:
            line: a list of string or string which keeps the command to run.
        """
        if type(line) == list:
            jobs = []
            for sub_command in line:
                p = multiprocessing.Process(
                    target=self.onecmd, args=(sub_command,))
                jobs.append(p)
                p.start()
            for job in jobs:
                job.join()
            return

        if line:
            print("Command: %s" % line)
        try:
            return cmd.Cmd.onecmd(self, line)
        except Exception as e:
            self._Print("%s: %s" % (type(e).__name__, e))
            return None

    # @Override
    def emptyline(self):
        """Ignores empty lines."""
        pass

    # @Override
    def default(self, line):
        """Handles unrecognized commands.

        Returns:
            True if receives EOF; otherwise delegates to default handler.
        """
        if line == "EOF":
            return self.do_exit(line)
        return cmd.Cmd.default(self, line)


def _ToPrintString(obj):
    """Converts an object to printable string on console.

    Args:
        obj: The object to be printed.
    """
    if isinstance(obj, (list, tuple, set)):
        return ",".join(str(x) for x in obj)
    return str(obj)
