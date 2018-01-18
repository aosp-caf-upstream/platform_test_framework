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

import json
import requests


class VtiEndpointClient(object):
    """VTI (Vendor Test Infrastructure) endpoint client.

    Attributes:
        _headers: A dictionary, containing HTTP request header information.
        _url: string, the base URL of an endpoint API.
    """

    def __init__(self, url):
        if not url.startswith(("https://")) and not url.startswith("http://"):
            url = "https://" + url
        if url.endswith("appspot.com"):
            url += "/_ah/api/"
        self._headers = {"content-type": "application/json",
                   "Accept-Charset": "UTF-8"}
        self._url = url

    def UploadBuildInfo(self, builds):
        """Uploads the given build information to VTI.

        Args:
            builds: a list of dictionaries, containing info about all new
                    builds found.

        Returns:
            True if successful, False otherwise.
        """
        url = self._url + "build_info/v1/set"
        fail = False
        for build in builds:
            response = requests.post(url, data=json.dumps(build),
                                     headers=self._headers)
            if response.status_code != requests.codes.ok:
                print "UploadDeviceInfo error: %s" % response
                fail = True
        if fail:
            return False
        return True

    def UploadDeviceInfo(self, hostname, devices):
        """Uploads the given device information to VTI.

        Args:
            hostname: string, the hostname of a target host.
            devices: a list of dicts, containing info about all detected
                     devices that are attached to the host.

        Returns:
            True if successful, False otherwise.
        """
        url = self._url + "host_info/v1/set"
        payload = {}
        payload["hostname"] = hostname
        payload["devices"] = []
        for device in devices:
            new_device = {
                "serial": device["serial"],
                "product": device["product"],
                "status": device["status"]}
            payload["devices"].append(new_device)
        response = requests.post(url, data=json.dumps(payload),
                                 headers=self._headers)
        if response.status_code != requests.codes.ok:
            print "UploadDeviceInfo error: %s" % response
            return False
        return True

    def UploadScheduleInfo(self, pbs):
        """Uploads the given schedule information to VTI.

        Args:
            pbs: a list of dicts, containing info about all task schedules.

        Returns:
            True if successful, False otherwise.
        """
        if pbs is None or len(pbs) == 0:
            return False

        url = self._url + "schedule_info/v1/clear"
        succ = True
        response = requests.post(
            url, data=json.dumps({"manifest_branch": "na"}),
            headers=self._headers)
        if response.status_code != requests.codes.ok:
            print("UploadDeviceInfo error: %s" % response)
            succ = False

        if not succ:
            return False

        url = self._url + "schedule_info/v1/set"
        for pb in pbs:
            schedule = {}
            schedule["manifest_branch"] = pb.manifest_branch
            for build_target in pb.build_target:
                schedule["build_target"] = [build_target.name]
                for test_schedule in build_target.test_schedule:
                    schedule["test_name"] = test_schedule.test_name
                    schedule["period"] = test_schedule.period
                    schedule["priority"] = test_schedule.priority
                    schedule["device"] = test_schedule.device
                    schedule["shards"] = test_schedule.shards
                    schedule["param"] = test_schedule.param
                    response = requests.post(url, data=json.dumps(schedule),
                                             headers=self._headers)
                    if response.status_code != requests.codes.ok:
                        print("UploadDeviceInfo error: %s" % response)
                        succ = False
        return succ

    def UploadLabInfo(self, pbs):
        """Uploads the given lab information to VTI.

        Args:
            pbs: a list of dicts, containing info about all known labs.

        Returns:
            True if successful, False otherwise.
        """
        if pbs is None or len(pbs) == 0:
            return

        url = self._url + "lab_info/v1/clear"
        succ = True
        response = requests.post(url, data=json.dumps({"name": "na"}),
                                 headers=self._headers)
        if response.status_code != requests.codes.ok:
            print "UploadDeviceInfo error: %s" % response
            succ = False

        if not succ:
            return False

        url = self._url + "lab_info/v1/set"
        for pb in pbs:
            lab = {}
            lab["name"] = pb.name
            lab["owner"] = pb.owner
            lab["host"] = []
            for host in pb.host:
                new_host = {}
                new_host["hostname"] = host.hostname
                new_host["ip"] = host.ip
                new_host["script"] = host.script
                lab["host"].append(new_host)
            response = requests.post(url, data=json.dumps(lab),
                                     headers=self._headers)
            if response.status_code != requests.codes.ok:
                print("UploadDeviceInfo error: %s" % response)
                succ = False
        return succ

    def LeaseJob(self, hostname):
        """Leases a job for the given host, 'hostname'.

        Args:
            hostname: string, the hostname of a target host.

        Returns:
            True if successful, False otherwise.
        """
        if not hostname:
            return None, {}

        url = self._url + "job_queue/v1/get"
        response = requests.post(url, data=json.dumps({"hostname": hostname}),
                                 headers=self._headers)
        if response.status_code != requests.codes.ok:
            print("LeaseJob error: %s" % response.status_code)
            return None, {}

        response_json = json.loads(response.text)
        if ("return_code" in response_json and
            response_json["return_code"] != "SUCCESS"):
            print("LeaseJob error: %s" % response_json)
            return None, {}

        if "jobs" not in response_json:
            print("LeaseJob jobs not found in response json %s" % response.text)
            return None, {}

        jobs = response_json["jobs"]
        if jobs and len(jobs) > 0:
            for job in jobs:
                return job["test_name"].split("/")[0], job
        return None, {}