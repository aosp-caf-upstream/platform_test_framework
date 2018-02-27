#
# Copyright (C) 2018 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import logging
import os
import shutil
import subprocess
import tempfile

from host_controller.command_processor import base_command_processor
from vts.runners.host import utils


class CommandTest(base_command_processor.BaseCommandProcessor):
    """Command processor for test command.

    Attributes:
        _result_dir: the path to the temporary result directory.
    """

    command = "test"
    command_detail = "Executes a command on TF."

    # @Override
    def SetUp(self):
        """Initializes the parser for test command."""
        self._result_dir = None
        self.arg_parser.add_argument(
            "--serial", "-s",
            default=None,
            help="The target device serial to run the command. "
            "A comma-separate list.")
        self.arg_parser.add_argument(
            "--test-exec-mode",
            default="subprocess",
            help="The target exec model.")
        self.arg_parser.add_argument(
            "--keep-result",
            action="store_true",
            help="Keep the path to the result in the console instance.")
        self.arg_parser.add_argument(
            "command",
            metavar="COMMAND",
            nargs="+",
            help="The command to be executed. If the command contains "
            "arguments starting with \"-\", place the command after "
            "\"--\" at end of line. format: plan -m module -t testcase")

    def _ClearResultDir(self):
        """Deletes all files in the result directory."""
        if self._result_dir is None:
            self._result_dir = tempfile.mkdtemp()
            return

        for file_name in os.listdir(self._result_dir):
            shutil.rmtree(os.path.join(self._result_dir, file_name))

    @staticmethod
    def _GenerateVtsCommand(bin_path, command, serials, result_dir=None):
        """Generates a vts-tradefed command.

        Args:
            bin_path: the path to vts-tradefed.
            command: a list of strings, the command arguments.
            serials: a list of strings, the serial numbers of the devices.
            result_dir: the path to the temporary directory where the result is
                        saved.

        Returns:
            a list of strings, the vts-tradefed command.
        """
        cmd = [bin_path, "run", "commandAndExit"]
        cmd.extend(str(c) for c in command)

        for serial in serials:
            cmd.extend(["-s", str(serial)])

        if result_dir:
            cmd.extend(["--log-file-path", result_dir, "--use-log-saver"])

        return cmd

    # @Override
    def Run(self, arg_line):
        """Executes a command using a VTS-TF instance.

        Args:
            arg_line: string, line of command arguments.
        """
        args = self.arg_parser.ParseLine(arg_line)
        if args.serial:
            serials = args.serial.split(",")
        elif self.console.GetSerials():
            serials = self.console.GetSerials()
        else:
            serials = []

        if args.test_exec_mode == "subprocess":
            if "vts" not in self.console.test_suite_info:
                 print("test_suite_info doesn't have 'vts': %s" %
                       self.console.test_suite_info)
                 return

            if args.keep_result:
                self._ClearResultDir()
                result_dir = self._result_dir
            else:
                result_dir = None

            cmd = self._GenerateVtsCommand(
                self.console.test_suite_info["vts"], args.command,
                serials, result_dir)

            print("Command: %s" % cmd)
            stdout = subprocess.check_output(cmd)
            logging.debug("stdout:\n%s", stdout)

            if result_dir:
                result_paths = [
                    os.path.join(dir_name, file_name) for
                    dir_name, file_name in utils.iterate_files(result_dir) if
                    file_name.startswith("log-result") and
                    file_name.endswith(".zip")]
                if len(result_paths) != 1:
                    logging.warning(
                        "Unexpected number of results: %s", result_paths)
                if len(result_paths) > 0:
                    self.console.test_results["vts"] = result_paths[0]
                else:
                    self.console.test_results.pop("vts", None)
        else:
            print("unsupported exec mode: %s", args.test_exec_mode)

    # @Override
    def TearDown(self):
        """Deletes the result directory."""
        if self._result_dir:
            shutil.rmtree(self._result_dir, ignore_errors=True)
