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

def EmitConsoleCommands(build_id="latest",
                        test_name="vts/vts",
                        shards=1,
                        serials=None,
                        **kwargs):
    """Runs a common VTS-on-GSI or CTS-on-GSI test.

    This uses a given device branch information and automatically
    selects a GSI branch and a test branch.
    """
    result = []

    if isinstance(build_target, list):
        build_target = build_target[0]

    result.append(
        "fetch --type=pab --branch=%s --target=%s --artifact_name=%s-img-%s.zip "
        "--build_id=%s --account_id=541462473" % (
            manifest_branch, build_target, build_target.split("-")[0],
            build_id if build_id != "latest" else "{build_id}", build_id),
    )
    result.append(
        "fetch --type=pab --branch=%s --target=%s --artifact_name=bootloader.img "
        "--build_id=%s --account_id=541462473" % (
            manifest_branch, build_target, build_id),
    )
    result.append(
        "fetch --type=pab --branch=%s --target=%s --artifact_name=radio.img "
        "--build_id=%s --account_id=541462473" % (
            manifest_branch, build_target, build_id),
    )

    result.append(
        "fetch --type=pab --branch=%s --target=aosp_arm64_ab-userdebug "
        "--artifact_name=aosp_arm64_ab-img-{build_id}.zip "
        "--build_id=latest" % gsi_branch,
    )
    result.append(
        "fetch --type=pab --branch=%s --target=%s "
        "--artifact_name=android-vts.zip "
        "--build_id=latest" % (test_branch, test_target),
    )

    shards = int(shards)
    result.append("info")
    result.append("gsispl --version_from_path=boot.img")
    test_name = test_name.split("/")[-1]
    if shards > 1:
        sub_commands = []
        if shards <= len(serials):
            for shard_index in range(shards):
                new_cmd_list = []
                new_cmd_list.append("flash --current --serial %s" %
                                    serials[shard_index])
                new_cmd_list.append(
                    "test -- %s --serial %s --shard-count %d "
                    "--shard-index %d" % (
                        test_name,
                        serials[shard_index],
                        shards, shard_index))
                sub_commands.append(new_cmd_list)
        result.append(sub_commands)
    else:
        result.append("flash --current")
        if serials:
            result.append(
                "test %s --shards %s --serial %s" % (test_name, shards,
                                                     ",".join(serials)))
        else:
            result.append(
                "test %s --shards %s" % (test_name, shards))

    return result
