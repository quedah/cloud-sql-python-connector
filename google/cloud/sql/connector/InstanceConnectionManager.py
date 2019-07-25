"""
Copyright 2019 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

# Importing libraries
import asyncio
import aiohttp
import concurrent
import googleapiclient
import googleapiclient.discovery
import google.auth
from google.auth.credentials import Credentials
import google.auth.transport.requests
import json
import OpenSSL
import threading
from typing import Dict, Union


# Custom utils import
from google.cloud.sql.connector.utils import generate_keys


class CloudSQLConnectionError(Exception):
    """
    Raised when the provided connection string is not formatted
    correctly.
    """

    def __init__(self, *args, **kwargs) -> None:
        super(CloudSQLConnectionError, self).__init__(self, *args, **kwargs)


class InstanceConnectionManager:
    """A class to manage the details of the connection, including refreshing the
    credentials.

    :param instance_connection_string:
        The Google Cloud SQL Instance's connection
        string.
    :type instance_connection_string: str
    :param loop:
        A new event loop for the refresh function to run in.
    :type loop: asyncio.AbstractEventLoop
    """

    # asyncio.AbstractEventLoop is used because the default loop,
    # SelectorEventLoop, is usable on both Unix and Windows but has limited
    # functionality on Windows. It is recommended to use ProactorEventLoop
    # while developing on Windows.
    _loop: asyncio.AbstractEventLoop = None
    _instance_connection_string: str = None
    _project: str = None
    _region: str = None
    _instance: str = None
    _credentials: Credentials = None
    _priv_key: str = None
    _pub_key: str = None
    _client_session: aiohttp.ClientSession = None
    _metadata: Dict[str, Union[Dict, str]] = None

    _mutex: threading.Lock = None

    _current: concurrent.futures.Future = None
    _next: concurrent.futures.Future = None

    def __init__(
        self, instance_connection_string: str, loop: asyncio.AbstractEventLoop
    ) -> None:
        print("born")
        # Validate connection string
        connection_string_split = instance_connection_string.split(":")

        if len(connection_string_split) == 3:
            self._instance_connection_string = instance_connection_string
            self._project = connection_string_split[0]
            self._region = connection_string_split[1]
            self._instance = connection_string_split[2]
        else:
            raise CloudSQLConnectionError(
                "Arg instance_connection_string must be in "
                + "format: project:region:instance."
            )
        self._loop = loop
        self._auth_init()
        self._priv_key, self._pub_key = generate_keys()

        self._mutex = threading.Lock()

        async def create_client_session():
            return aiohttp.ClientSession()

        self._client_session = asyncio.run_coroutine_threadsafe(
            create_client_session(), loop=self._loop
        ).result()

        # set current to future InstanceMetadata
        # set next to the future future InstanceMetadata

    def __del__(self):
        """Deconstructor to make sure ClientSession is closed and tasks have
        finished to have a graceful exit.
        """
        print("deconstructing")
        if self._current is not None:
            print("waiting for current")
            self._current.result()

        if self._client_session is not None and not self._client_session.closed:
            print("killing client session")
            close_future = asyncio.run_coroutine_threadsafe(
                self._client_session.close(), loop=self._loop
            )
            close_future.result()

        print("all dead")

    @staticmethod
    async def _get_metadata(
        client_session: aiohttp.ClientSession,
        credentials: Credentials,
        project: str,
        instance: str,
    ) -> Dict[str, Union[Dict, str]]:
        """Requests metadata from the Cloud SQL Instance
        and returns a dictionary containing the IP addresses and certificate
        authority of the Cloud SQL Instance.

        :type credentials: google.oauth2.service_account.Credentials
        :param service:
            A credentials object created from the google-auth Python library.
            Must have the SQL Admin API scopes. For more info check out
            https://google-auth.readthedocs.io/en/latest/.

        :type project: str
        :param project:
            A string representing the name of the project.

        :type inst_name: str
        :param project: A string representing the name of the instance.

        :rtype: Dict[str: Union[Dict, str]]
        :returns: Returns a dictionary containing a dictionary of all IP
              addresses and their type and a string representing the
              certificate authority.

        :raises TypeError: If any of the arguments are not the specified type.
        """

        if (
            not isinstance(credentials, Credentials)
            or not isinstance(project, str)
            or not isinstance(instance, str)
        ):
            raise TypeError(
                "Arguments must be as follows: "
                + "service (googleapiclient.discovery.Resource), "
                + "proj_name (str) and inst_name (str)."
            )

        if not credentials.valid:
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)

        headers = {
            "Authorization": "Bearer {}".format(credentials.token),
            "Content-Type": "application/json",
        }

        url = "https://www.googleapis.com/sql/v1beta4/projects/{}/instances/{}".format(
            project, instance
        )
        print("requesting metadata")
        resp = await client_session.get(url, headers=headers, raise_for_status=True)
        ret_dict = json.loads(await resp.text())

        metadata = {
            "ip_addresses": {
                ip["type"]: ip["ipAddress"] for ip in ret_dict["ipAddresses"]
            },
            "server_ca_cert": ret_dict["serverCaCert"]["cert"],
        }

        return metadata

    @staticmethod
    async def _get_ephemeral(
        client_session: aiohttp.ClientSession,
        credentials: Credentials,
        project: str,
        instance: str,
        pub_key: str,
    ) -> str:
        """Asynchronously requests an ephemeral certificate from the Cloud SQL Instance.

        Args:
            credentials (google.oauth2.service_account.Credentials): A credentials object
              created from the google-auth library. Must be
              using the SQL Admin API scopes. For more info, check out
              https://google-auth.readthedocs.io/en/latest/.
            project (str): A string representing the name of the project.
            instance (str): A string representing the name of the instance.
            pub_key (str): A string representing PEM-encoded RSA public key.

        Returns:
            str
              An ephemeral certificate from the Cloud SQL instance that allows
              authorized connections to the instance.

        Raises:
            TypeError: If one of the arguments passed in is None.
        """

        print("life is ephemeral")

        if (
            not isinstance(credentials, Credentials)
            or not isinstance(project, str)
            or not isinstance(instance, str)
            or not isinstance(pub_key, str)
        ):
            raise TypeError("Cannot take None as an argument.")

        if not credentials.valid:
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)

        headers = {
            "Authorization": "Bearer {}".format(credentials.token),
            "Content-Type": "application/json",
        }

        url = "https://www.googleapis.com/sql/v1beta4/projects/{}/instances/{}/createEphemeral".format(
            project, instance
        )

        data = {"public_key": pub_key}

        # print(client_session)
        if client_session is not None:
            resp = await client_session.post(
                url, headers=headers, json=data, raise_for_status=True
            )
        else:
            async with aiohttp.ClientSession() as cs:
                resp = await cs.post(
                    url, headers=headers, json=data, raise_for_status=True
                )

        ret_dict = json.loads(await resp.text())

        # print(type(ret_dict["cert"]))

        return ret_dict["cert"]

    async def _get_instance_data(
        self,
        metadata_task: concurrent.futures.Future,
        ephemeral_task: concurrent.futures.Future,
    ) -> Dict[str, Union[OpenSSL.SSL.Context, Dict]]:
        """Asynchronous function that takes in the futures for the ephemeral certificate
        and the instance metadata and generates an OpenSSL context object.

        :type ephemeral_task: concurrent.futures.Future
        :param ephemeral_task:
            A future representing the ephemeral certificate returned from _get_ephemeral.

        :type metadata_task: concurrent.futures.Future
        :param metadata_task:
            A future representing the instance metadata returend from _get_metdata.

        :rtype: Dict[str, Union[OpenSSL.SSL.Context, Dict]]
        :returns: A dict containing an OpenSSL context that is created using the requested ephemeral certificate
            instance metadata and a dict that contains all the instance's IP addresses.
        """

        print("Creating context")
        metadata = await metadata_task
        ephemeral_cert = await ephemeral_task

        PEM = OpenSSL.crypto.FILETYPE_PEM

        pkey = OpenSSL.crypto.load_privatekey(PEM, self._priv_key)
        public_cert = OpenSSL.crypto.load_certificate(PEM, ephemeral_cert.encode())
        trusted_cert = OpenSSL.crypto.load_certificate(
            PEM, metadata["server_ca_cert"].encode()
        )

        ctx = OpenSSL.SSL.Context(OpenSSL.SSL.TLSv1_METHOD)
        ctx.use_privatekey(pkey)
        ctx.use_certificate(public_cert)
        ctx.check_privatekey()
        ctx.get_cert_store().add_cert(trusted_cert)

        instance_data = {"ssl_context": ctx, "ip_addresses": metadata["ip_addresses"]}

        return instance_data

    def _threadsafe_refresh(self, future) -> None:
        """A threadsafe way to update the current instance data and the
        future instance data. Only meant to be called as a callback.

        :type future: asyncio.Future
        :param future: The future passed in by add_done_callback.
        """
        print("_threadsafe_refresh\n-----------------------")
        with self._mutex:
            self._current = future.result()
            self._next = self._loop.create_task(self._schedule_refresh(10))

    def _auth_init(self) -> None:
        """Creates and assigns a Google Python API service object for
        Google Cloud SQL Admin API.
        """

        credentials, project = google.auth.default()
        scoped_credentials = credentials.with_scopes(
            [
                "https://www.googleapis.com/auth/sqlservice.admin",
                "https://www.googleapis.com/auth/cloud-platform",
            ]
        )

        cloudsql = googleapiclient.discovery.build(
            "sqladmin", "v1beta4", credentials=scoped_credentials
        )

        self._credentials = scoped_credentials
        self._cloud_sql_service = cloudsql

    def _perform_refresh(self) -> asyncio.Task:
        """Retrieves instance metadata and ephemeral certificate from the
        Cloud SQL Instance.

        :rtype: asyncio.Task
        :returns: An awaitable representing the creation of an SSLcontext.
        """

        print("refreshing")

        # schedule get_metadata and get_ephemeral as tasks
        metadata_future = self._loop.create_task(
            self._get_metadata(
                self._client_session, self._credentials, self._project, self._instance
            )
        )

        ephemeral_future = self._loop.create_task(
            self._get_ephemeral(
                self._client_session,
                self._credentials,
                self._project,
                self._instance,
                self._pub_key.decode("UTF-8"),
            )
        )

        instance_data_task = self._loop.create_task(
            self._get_instance_data(metadata_future, ephemeral_future)
        )
        instance_data_task.add_done_callback(self._threadsafe_refresh)

        return instance_data_task

    async def _schedule_refresh(self, delay: int):
        print("schedling")
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledException:
            return None

        return self._perform_refresh()
