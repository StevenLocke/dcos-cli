import base64
import fnmatch
import itertools
import json
import os
import signal
import sys
import threading

from dcos import config, http, recordio, util
from dcos.errors import DCOSException, DCOSHTTPException
from functools import partial
from queue import Queue
from six.moves import urllib

logger = util.get_logger(__name__)

def get_master(dcos_client=None):
    """Create a Master object using the url stored in the
    'core.mesos_master_url' property if it exists.  Otherwise, we use
    the `core.dcos_url` property

    :param dcos_client: DCOSClient
    :type dcos_client: DCOSClient | None
    :returns: master state object
    :rtype: Master
    """

    dcos_client = dcos_client or DCOSClient()
    return Master(dcos_client.get_master_state())


class DCOSClient(object):
    """Client for communicating with DC/OS"""

    def __init__(self):
        toml_config = config.get_config()

        self._dcos_url = config.get_config_val("core.dcos_url", toml_config)
        if self._dcos_url is None:
            raise config.missing_config_exception(['core.dcos_url'])
        self._mesos_master_url = config.get_config_val(
            'core.mesos_master_url', toml_config)

        self._timeout = config.get_config_val('core.timeout', toml_config)

    def get_dcos_url(self, path):
        """ Create a DC/OS URL

        :param path: the path suffix of the URL
        :type path: str
        :returns: DC/OS URL
        :rtype: str
        """

        return urllib.parse.urljoin(self._dcos_url, path)

    def master_url(self, path):
        """ Create a master URL

        :param path: the path suffix of the desired URL
        :type path: str
        :returns: URL that hits the master
        :rtype: str
        """

        base_url = (self._mesos_master_url or
                    urllib.parse.urljoin(self._dcos_url, 'mesos/'))
        return urllib.parse.urljoin(base_url, path)

    def slave_url(self, slave_id, private_url, path):
        """Create a slave URL

        :param slave_id: slave ID
        :type slave_id: str
        :param private_url: The slave's private URL derived from its
                            pid.  Used when we're accessing mesos
                            directly, rather than through DC/OS.
        :type private_url: str
        :param path: the path suffix of the desired URL
        :type path: str
        :returns: URL that hits the master
        :rtype: str

        """

        if self._mesos_master_url:
            return urllib.parse.urljoin(private_url, path)
        else:
            return urllib.parse.urljoin(self._dcos_url,
                                        'slave/{}/{}'.format(slave_id, path))

    def get_master_state(self):
        """Get the Mesos master state json object

        :returns: Mesos' master state json object
        :rtype: dict
        """

        url = self.master_url('master/state.json')
        return http.get(url, timeout=self._timeout).json()

    def get_slave_state(self, slave_id, private_url):
        """Get the Mesos slave state json object

        :param slave_id: slave ID
        :type slave_id: str
        :param private_url: The slave's private URL derived from its
                            pid.  Used when we're accessing mesos
                            directly, rather than through DC/OS.
        :type private_url: str
        :returns: Mesos' master state json object
        :rtype: dict

        """

        url = self.slave_url(slave_id, private_url, 'state.json')
        return http.get(url, timeout=self._timeout).json()

    def get_state_summary(self):
        """Get the Mesos master state summary json object

        :returns: Mesos' master state summary json object
        :rtype: dict
        """

        url = self.master_url('master/state-summary')
        return http.get(url, timeout=self._timeout).json()

    def slave_file_read(self, slave_id, private_url, path, offset, length):
        """See the master_file_read() docs

        :param slave_id: slave ID
        :type slave_id: str
        :param path: absolute path to read
        :type path: str
        :param private_url: The slave's private URL derived from its
                            pid.  Used when we're accessing mesos
                            directly, rather than through DC/OS.
        :type private_url: str
        :param offset: start byte location, or -1.  -1 means read no data, and
                       is used to fetch the size of the file in the response's
                       'offset' parameter.
        :type offset: int
        :param length: number of bytes to read, or -1.  -1 means read the whole
                       file
        :type length: int
        :returns: files/read.json response
        :rtype: dict

        """

        url = self.slave_url(slave_id,
                             private_url,
                             'files/read.json')
        params = {'path': path,
                  'length': length,
                  'offset': offset}
        return http.get(url, params=params, timeout=self._timeout).json()

    def master_file_read(self, path, length, offset):
        """This endpoint isn't well documented anywhere, so here is the spec
        derived from the mesos source code:

        request format:
        {
            path: absolute path to read
            offset: start byte location, or -1.  -1 means read no data, and
                    is used to fetch the size of the file in the response's
                    'offset' parameter.
            length: number of bytes to read, or -1.  -1 means read the whole
                    file.
        }

        response format:
        {
            data: file data.  Empty if a request.offset=-1.  Could be
                  smaller than request.length if EOF was reached, or if (I
                  believe) request.length is larger than the length
                  supported by the server (16 pages I believe).

            offset: the offset value from the request, or the size of the
                    file if the request offset was -1 or >= the file size.
        }

        :param path: absolute path to read
        :type path: str
        :param offset: start byte location, or -1.  -1 means read no data, and
                       is used to fetch the size of the file in the response's
                       'offset' parameter.
        :type offset: int
        :param length: number of bytes to read, or -1.  -1 means read the whole
                       file
        :type length: int
        :returns: files/read.json response
        :rtype: dict
        """

        url = self.master_url('files/read.json')
        params = {'path': path,
                  'length': length,
                  'offset': offset}
        return http.get(url, params=params, timeout=self._timeout).json()

    def shutdown_framework(self, framework_id):
        """Shuts down a Mesos framework

        :param framework_id: ID of the framework to shutdown
        :type framework_id: str
        :returns: None
        """

        logger.info('Shutting down framework {}'.format(framework_id))

        data = 'frameworkId={}'.format(framework_id)

        url = self.master_url('master/teardown')

        # In Mesos 0.24, /shutdown was removed.
        # If /teardown doesn't exist, we try /shutdown.
        try:
            http.post(url, data=data, timeout=self._timeout)
        except DCOSHTTPException as e:
            if e.response.status_code == 404:
                url = self.master_url('master/shutdown')
                http.post(url, data=data, timeout=self._timeout)
            else:
                raise

    def metadata(self):
        """ GET /metadata

        :returns: /metadata content
        :rtype: dict
        """
        url = self.get_dcos_url('metadata')
        return http.get(url, timeout=self._timeout).json()

    def browse(self, slave, path):
        """ GET /files/browse.json

        Request
        path:...  # path to run ls on

        Response
        [
          {
            path:  # full path to file
            nlink:
            size:
            mtime:
            mode:
            uid:
            gid:
          }
        ]

        :param slave: slave to issue the request on
        :type slave: Slave
        :returns: /files/browse.json response
        :rtype: dict
        """

        url = self.slave_url(slave['id'],
                             slave.http_url(),
                             'files/browse.json')
        return http.get(url, params={'path': path}).json()


class MesosDNSClient(object):
    """ Mesos-DNS client

    :param url: mesos-dns URL
    :type url: str
    """
    def __init__(self, url=None):
        self.url = url or urllib.parse.urljoin(
            config.get_config_val('core.dcos_url'), '/mesos_dns/')

    def _path(self, path):
        """ Construct a full path

        :param path: path suffix
        :type path: str
        :returns: full path
        :rtype: str
        """
        return urllib.parse.urljoin(self.url, path)

    def hosts(self, host):
        """ GET v1/hosts/<host>

        :param host: host
        :type host: str
        :returns: {'ip', 'host'} dictionary
        :rtype: dict(str, str)
        """
        url = self._path('v1/hosts/{}'.format(host))
        return http.get(url, headers={}).json()


class Master(object):
    """Mesos Master Model

    :param state: Mesos master's state.json
    :type state: dict
    """

    def __init__(self, state):
        self._state = state
        self._frameworks = {}
        self._slaves = {}

    def state(self):
        """Returns master's master/state.json.

        :returns: state.json
        :rtype: dict
        """

        return self._state

    def slave(self, fltr):
        """Returns the slave that has `fltr` in its ID. If any slaves
        are an exact match, returns that task, id not raises a
        DCOSException if there is not exactly one such slave.

        :param fltr: filter string
        :type fltr: str
        :returns: the slave that has `fltr` in its ID
        :rtype: Slave
        """

        slaves = self.slaves(fltr)

        if len(slaves) == 0:
            raise DCOSException('No slave found with ID "{}".'.format(fltr))

        elif len(slaves) > 1:

            exact_matches = [s for s in slaves if s['id'] == fltr]
            if len(exact_matches) == 1:
                return exact_matches[0]

            else:
                matches = ['\t{0}'.format(s['id']) for s in slaves]
                raise DCOSException(
                    "There are multiple slaves with that ID. " +
                    "Please choose one:\n{}".format('\n'.join(matches)))

        else:
            return slaves[0]

    def task(self, fltr, completed=False):
        """Returns the task with `fltr` in its ID.  Raises a DCOSException if
        there is not exactly one such task.

        :param fltr: filter string
        :type fltr: str
        :returns: the task that has `fltr` in its ID
        :param completed: also include completed tasks
        :type completed: bool
        :rtype: Task
        """

        tasks = self.tasks(fltr, completed)

        if len(tasks) == 0:
            raise DCOSException(
                'Cannot find a task with ID containing "{}"'.format(fltr))

        elif len(tasks) > 1:
            msg = [("There are multiple tasks with ID matching [{}]. " +
                    "Please choose one:").format(fltr)]
            msg += ["\t{0}".format(t["id"]) for t in tasks]
            raise DCOSException('\n'.join(msg))

        else:
            return tasks[0]

    def framework(self, framework_id):
        """Returns a framework by ID

        :param framework_id: the framework's ID
        :type framework_id: str
        :returns: the framework
        :rtype: Framework
        """

        for f in self._framework_dicts(True, True):
            if f['id'] == framework_id:
                return self._framework_obj(f)
        return None

    def slaves(self, fltr=""):
        """Returns those slaves that have `fltr` in their 'id'

        :param fltr: filter string
        :type fltr: str
        :returns: Those slaves that have `fltr` in their 'id'
        :rtype: [Slave]
        """

        return [self._slave_obj(slave)
                for slave in self.state()['slaves']
                if fltr in slave['id']]

    def tasks(self, fltr=None, completed=False):
        """Returns tasks running under the master

        :param fltr: May be None, a substring or regex. None returns all tasks,
                     else return tasks whose 'id' matches `fltr`.
        :type fltr: str | None
        :param completed: also include completed tasks
        :type completed: bool
        :returns: a list of tasks
        :rtype: [Task]
        """

        keys = ['tasks']
        if completed:
            keys.extend(['completed_tasks'])

        tasks = []
        for framework in self._framework_dicts(completed, completed):
            for task in _merge(framework, keys):
                if fltr is None or \
                        fltr in task['id'] or \
                        fnmatch.fnmatchcase(task['id'], fltr):
                    task = self._framework_obj(framework).task(task['id'])
                    tasks.append(task)

        return tasks

    # TODO: Currently waiting on this mapping in state.json to be present.
    # This mapping is only present in DC/OS 1.9+
    def get_container_id(self, task_id):
        """Returns the container ID for a task ID matching `task_id`

        :param task_id: The task ID which will be mapped to container ID
        :type task_id: str
        :returns: the container id associated with task id
        :rtype: str
        """
        if not task_id:
            #TODO, make this more detailed
            raise DCOSException("No task id passed to get container_id")


        #Loop through tasks in frameworks
        if 'frameworks' in self.state():
            for framework in self.state()['frameworks']:
                if 'tasks' in framework:
                    for task in framework['tasks']:

                        #Check if we've found the right task, then get its value
                        if 'id' in task and task['id'] == task_id:
                            if 'statuses' in task  and 'container_status' in task['statuses'][0]:
                                if 'container_id' in task['statuses'][0]['container_status']:
                                    container_status = task['statuses'][0]['container_status']
                                    if 'container_id' in container_status and 'value' in container_status['container_id']:
                                        return container_status['container_id']['value']

        raise DCOSException(
            "No container found for the specified task."
            " It might still be spinning up."
            " Please try again later.")


    def frameworks(self, inactive=False, completed=False):
        """Returns a list of all frameworks

        :param inactive: also include inactive frameworks
        :type inactive: bool
        :param completed: also include completed frameworks
        :type completed: bool
        :returns: a list of frameworks
        :rtype: [Framework]
        """

        return [self._framework_obj(framework)
                for framework in self._framework_dicts(inactive, completed)]

    @util.duration
    def fetch(self, path, **kwargs):
        """GET the resource located at `path`

        :param path: the URL path
        :type path: str
        :param **kwargs: http.get kwargs
        :type **kwargs: dict
        :returns: the response object
        :rtype: Response
        """

        url = urllib.parse.urljoin(self._base_url(), path)
        return http.get(url, **kwargs)

    def _slave_obj(self, slave):
        """Returns the Slave object corresponding to the provided `slave`
        dict.  Creates it if it doesn't exist already.

        :param slave: slave
        :type slave: dict
        :returns: Slave
        :rtype: Slave
        """

        if slave['id'] not in self._slaves:
            self._slaves[slave['id']] = Slave(slave, None, self)
        return self._slaves[slave['id']]

    def _framework_obj(self, framework):
        """Returns the Framework object corresponding to the provided `framework`
        dict.  Creates it if it doesn't exist already.

        :param framework: framework
        :type framework: dict
        :returns: Framework
        :rtype: Framework
        """

        if framework['id'] not in self._frameworks:
            self._frameworks[framework['id']] = Framework(framework, self)
        return self._frameworks[framework['id']]

    def _framework_dicts(self, inactive=False, completed=False):
        """Returns a list of all frameworks as their raw dictionaries

        :param inactive: also include inactive frameworks
        :type inactive: bool
        :param completed: also include completed frameworks
        :type completed: bool
        :returns: a list of frameworks
        """

        if completed:
            for framework in self.state()['completed_frameworks']:
                yield framework

        for framework in self.state()['frameworks']:
            if inactive or framework['active']:
                yield framework


class Slave(object):
    """Mesos Slave Model

    :param short_state: slave's entry from the master's state.json
    :type short_state: dict
    :param state: slave's state.json
    :type state: dict | None
    :param master: slave's master
    :type master: Master
    """

    def __init__(self, short_state, state, master):
        self._short_state = short_state
        self._state = state
        self._master = master

    def state(self):
        """Get the slave's state.json object.  Fetch it if it's not already
        an instance variable.

        :returns: This slave's state.json object
        :rtype: dict
        """

        if not self._state:
            self._state = DCOSClient().get_slave_state(self['id'],
                                                       self.http_url())
        return self._state

    def http_url(self):
        """
        :returns: The private HTTP URL of the slave.  Derived from the
                  `pid` property.
        :rtype: str
        """

        parsed_pid = parse_pid(self['pid'])
        return 'http://{}:{}'.format(parsed_pid[1], parsed_pid[2])

    def _framework_dicts(self):
        """Returns the framework dictionaries from the state.json dict

        :returns: frameworks
        :rtype: [dict]
        """

        return _merge(self.state(), ['frameworks', 'completed_frameworks'])

    def executor_dicts(self):
        """Returns the executor dictionaries from the state.json

        :returns: executors
        :rtype: [dict]
        """

        iters = [_merge(framework, ['executors', 'completed_executors'])
                 for framework in self._framework_dicts()]
        return itertools.chain(*iters)

    def __getitem__(self, name):
        """Support the slave[attr] syntax

        :param name: attribute to get
        :type name: str
        :returns: the value for this attribute in the underlying
                  slave dictionary
        :rtype: object
        """

        return self._short_state[name]


class Framework(object):
    """ Mesos Framework Model

    :param framework: framework properties
    :type framework: dict
    :param master: framework's master
    :type master: Master
    """

    def __init__(self, framework, master):
        self._framework = framework
        self._master = master
        self._tasks = {}  # id->Task map

    def task(self, task_id):
        """Returns a task by id

        :param task_id: the task's id
        :type task_id: str
        :returns: the task
        :rtype: Task
        """

        for task in _merge(self._framework, ['tasks', 'completed_tasks']):
            if task['id'] == task_id:
                return self._task_obj(task)
        return None

    def _task_obj(self, task):
        """Returns the Task object corresponding to the provided `task`
        dict.  Creates it if it doesn't exist already.

        :param task: task
        :type task: dict
        :returns: Task
        :rtype: Task
        """

        if task['id'] not in self._tasks:
            self._tasks[task['id']] = Task(task, self._master)
        return self._tasks[task['id']]

    def dict(self):
        return self._framework

    def __getitem__(self, name):
        """Support the framework[attr] syntax

        :param name: attribute to get
        :type name: str
        :returns: the value for this attribute in the underlying
                  framework dictionary
        :rtype: object
        """

        return self._framework[name]


class Task(object):
    """Mesos Task Model.

    :param task: task properties
    :type task: dict
    :param master: mesos master
    :type master: Master
    """

    def __init__(self, task, master):
        self._task = task
        self._master = master

    def dict(self):
        """
        :returns: dictionary representation of this Task
        :rtype: dict
        """

        return self._task

    def framework(self):
        """Returns this task's framework

        :returns: task's framework
        :rtype: Framework
        """

        return self._master.framework(self["framework_id"])

    def slave(self):
        """Returns the task's slave

        :returns: task's slave
        :rtype: Slave
        """

        return self._master.slave(self["slave_id"])

    def user(self):
        """Task owner

        :returns: task owner
        :rtype: str
        """

        return self.framework()['user']

    def executor(self):
        """ Returns this tasks' executor

        :returns: task's executor
        :rtype: dict
        """
        for executor in self.slave().executor_dicts():
            tasks = _merge(executor,
                           ['completed_tasks',
                            'tasks',
                            'queued_tasks'])
            if any(task['id'] == self['id'] for task in tasks):
                return executor
        return None

    def directory(self):
        """ Sandbox directory for this task

        :returns: path to task's sandbox
        :rtype: str
        """

        return self.executor()['directory']

    def __getitem__(self, name):
        """Support the task[attr] syntax

        :param name: attribute to get
        :type name: str
        :returns: the value for this attribute in the underlying
                  task dictionary
        :rtype: object
        """

        return self._task[name]


class MesosFile(object):
    """File-like object that is backed by a remote slave or master file.
    Uses the files/read.json endpoint.

    If `task` is provided, the file host is `task.slave()`.  If
    `slave` is provided, the file host is `slave`.  It is invalid to
    provide both.  If neither is provided, the file host is the
    leading master.

    :param path: file's path, relative to the sandbox if `task` is given
    :type path: str
    :param task: file's task
    :type task: Task | None
    :param slave: slave where the file lives
    :type slave: Slave | None
    :param dcos_client: client to use for network requests
    :type dcos_client: DCOSClient | None

    """

    def __init__(self, path, task=None, slave=None, dcos_client=None):
        if task and slave:
            raise ValueError(
                "You cannot provide both `task` and `slave` " +
                "arguments.  `slave` is understood to be `task.slave()`")

        if slave:
            self._slave = slave
        elif task:
            self._slave = task.slave()
        else:
            self._slave = None

        self._task = task
        self._path = path
        self._dcos_client = dcos_client or DCOSClient()
        self._cursor = 0

    def size(self):
        """Size of the file

        :returns: size of the file
        :rtype: int
        """

        params = self._params(0, offset=-1)
        return self._fetch(params)["offset"]

    def seek(self, offset, whence=os.SEEK_SET):
        """Seek to the provided location in the file.

        :param offset: location to seek to
        :type offset: int
        :param whence: determines whether `offset` represents a
                       location that is absolute, relative to the
                       beginning of the file, or relative to the end
                       of the file
        :type whence: os.SEEK_SET | os.SEEK_CUR | os.SEEK_END
        :returns: None
        :rtype: None
        """

        if whence == os.SEEK_SET:
            self._cursor = 0 + offset
        elif whence == os.SEEK_CUR:
            self._cursor += offset
        elif whence == os.SEEK_END:
            self._cursor = self.size() + offset
        else:
            raise ValueError(
                "Unexpected value for `whence`: {}".format(whence))

    def tell(self):
        """ The current cursor position.

        :returns: the current cursor position
        :rtype: int
        """

        return self._cursor

    def read(self, length=None):
        """Reads up to `length` bytes, or the entire file if `length` is None.

        :param length: number of bytes to read
        :type length: int | None
        :returns: data read
        :rtype: str
        """

        data = ''
        while length is None or length - len(data) > 0:
            chunk_length = -1 if length is None else length - len(data)
            chunk = self._fetch_chunk(chunk_length)
            if chunk == '':
                break
            data += chunk

        return data

    def _host_path(self):
        """ The absolute path to the file on slave.

        :returns: the absolute path to the file on slave
        :rtype: str
        """

        if self._task:
            directory = self._task.directory().rstrip('/')
            executor = self._task.executor()
            # executor.type is currently used only by pods. All tasks in a pod
            # share an executor, so if this is a pod, get the task logs instead
            # of the executor logs
            if executor.get('type') == "DEFAULT":
                task_id = self._task.dict().get('id')
                return directory + '/tasks/{}/'.format(task_id) + self._path
            else:
                return directory + '/' + self._path
        else:
            return self._path

    def _params(self, length, offset=None):
        """GET parameters to send to files/read.json.  See the MesosFile
        docstring for full information.

        :param length: number of bytes to read
        :type length: int
        :param offset: start location.  if None, will use the location
                       of the current file cursor
        :type offset: int
        :returns: GET parameters
        :rtype: dict
        """

        if offset is None:
            offset = self._cursor

        return {
            'path': self._host_path(),
            'offset': offset,
            'length': length
        }

    def _fetch_chunk(self, length, offset=None):
        """Fetch data from files/read.json

        :param length: number of bytes to fetch
        :type length: int
        :param offset: start location.  If not None, this file's
                       cursor is set to `offset`
        :type offset: int
        :returns: data read
        :rtype: str
        """

        if offset is not None:
            self.seek(offset, os.SEEK_SET)

        params = self._params(length)
        data = self._fetch(params)["data"]
        self.seek(len(data), os.SEEK_CUR)
        return data

    def _fetch(self, params):
        """Fetch data from files/read.json

        :param params: GET parameters
        :type params: dict
        :returns: response dict
        :rtype: dict
        """

        if self._slave:
            return self._dcos_client.slave_file_read(self._slave['id'],
                                                     self._slave.http_url(),
                                                     **params)
        else:
            return self._dcos_client.master_file_read(**params)

    def __str__(self):
        """String representation of the file: <task_id:file_path>

        :returns: string representation of the file
        :rtype: str
        """

        if self._task:
            return "task:{0}:{1}".format(self._task['id'], self._path)
        elif self._slave:
            return "slave:{0}:{1}".format(self._slave['id'], self._path)
        else:
            return "master:{0}".format(self._path)


class TaskIO(object):
    """Object allowing interaction with the Mesos Agent exec functionality.

    :param task: task ID
    :type task: str
    :param interactive: Create a third persistant connection for streaming
    STDIN
    :type interactive: bool
    :param cmd: the command to fork inside the container
    :type cmd: str
    :param args: Additional arguments for the command
    :type args: str
    """

    def __init__(self, task_id, interactive=False, cmd=None, args=None):
        if not task_id:
            raise DCOSException(
                "Must provide <task ID>, example:"
                " `dcos task exec <task ID> <cmd>")

        # Get the ContainerID and Agent URL assciated with
        # the given Task ID.
        client = DCOSClient()
        master = get_master(client)

        container_id = master.get_container_id(task_id)
        if not container_id:
            raise DCOSException(
                "Container ID for task {} not found.".format(task_id))

        self.container_id = container_id

        # Get the URL to the agent which is running the task
        task_obj = master.task(task_id)
        self.agent_url = client.slave_url(task_obj.slave()['id'], "", "api/v1")

        self.interactive = interactive
        self.cmd = cmd
        self.args = args

        self.encoder = recordio.Encoder(lambda s: bytes(json.dumps(s, ensure_ascii=False), "UTF-8"))
        self.decoder = recordio.Decoder(lambda s: json.loads(s.decode("UTF-8")))

        self.input_queue = Queue()
        self.output_queue = Queue()
        self.exit_queue = Queue()

        self.exit_event = threading.Event()
        self.exit_event.clear()

        if not interactive:
            self.input_queue = None

    def IORunner(self):
        """IORunner handles running the helper methods in this
        class which enable streaming of STDIN/OUT/ERR back and
        forth between the CLI client and Mesos Agent API
        """

        launch_container_thread = threading.Thread(
            target=self._launch_container_session)
        launch_container_thread.daemon = True
        launch_container_thread.start()

        out_thread = threading.Thread(
            target=self._output_thread)
        out_thread.daemon = True
        out_thread.start()

        if self.interactive:
            # Local input thread
            in_thread = threading.Thread(target=self._input_thread)
            in_thread.daemon = True
            in_thread.start()

            # Remote input thread
            in_stream_thread = threading.Thread(
                target=self._attach_input_stream)
            in_stream_thread.daemon = True
            in_stream_thread.start()

        try:
            self.exit_queue.get(block=True)
        except KeyboardInterrupt:
            pass

    def _initialize_launch_container_message(self):
        """Decides if this is an `attach` or `exec` on the basis that
        an exec needs a cmd, and an attach does not.

        :rtype: byte string - JSON value for initializing the output stream
        """

        init_output_attach_msg = {}
        init_output_attach_msg['type'] = "LAUNCH_NESTED_CONTAINER_SESSION"
        init_output_attach_msg['launch_nested_container_session'] = {
            'container_id': {
                'value': self.container_id,
            },
            'command': {
                'value': self.cmd,
                'arguments': self.args,
                'shell': False,
            }
        }

        return self.encoder.encode(init_output_attach_msg)

    def get_chunked_msg(self, fileno):
        """Reads and returns a message from the given fileno.

        :param fileno: file number to read from
        :type fileno: int
        :rtype: byte string - message read from the fileno
        """

        chunk_size = os.read(fileno, 2)
        while chunk_size[-2:] != b"\r\n":
            chunk_size += os.read(fileno, 1)
        chunk_size = int(chunk_size[:-2], 16)

        chunk = os.read(fileno, chunk_size)
        os.read(fileno, 2)
        return chunk


    def _launch_container_session(self):
        """Sends a request to the Mesos Agent API to attach the
        STDOUT stream of an already running container.
        """

        jsonified_output_attach_msg = self._initialize_launch_container_message()
        
        req_extra_args = {
            'stream': True,
            'headers': {
                'content-type': 'application/json'
            }
        }

        response = http.post(
            self.agent_url,
            data=jsonified_output_attach_msg,
            **req_extra_args)

        self._attach_output_stream(response.raw.fileno())


    def _attach_output_stream(self, fileno):
        """Gets data from the given fileno and places the
        returned messages into our output_queue.

        :param fileno: file number to read from
        :type fileno: int
        """

        while True:
            try:
                chunk = self.get_chunked_msg(fileno)
                if len(chunk) == 0:
                    break

                records = self.decoder.decode(chunk)

                if records:
                    for r in records:
                        if r['type'] == 'DATA':
                            self.output_queue.put(r['data'])

            except:
                raise DCOSException('Invalid message type passed to _attach_output_stream')

        self.output_queue.join()
        self.exit_event.wait()
        self.exit_queue.put(None)

    def _attach_input_stream(self):
        """Sends data from _input_streamer to the agent_url
        """

        def _input_streamer():
            """Creates the initial ATTACH_CONTAINER_INPUT message,
            then yields a message from the input_queue on each subsequent call
            """

            init_input_attach_msg = {}
            init_input_attach_msg['type'] = 'ATTACH_CONTAINER_INPUT'
            init_input_attach_msg['attach_container_input'] = {
                'type': 'CONTAINER_ID',
                'container_id': {
                    'value': self.container_id,
                }
            }

            send_init = True
            while True:
                item = self.input_queue.get()
                if send_init:
                    send_init = False
                    yield self.encoder.encode(init_input_attach_msg)
                yield item

        req_extra_args = {
            'stream': True,
            'headers': {
                'connection': 'keep-alive',
                'content-type': 'application/json',
                'transfer-encoding': 'chunked'}}

        response = http.post(
            self.agent_url,
            data=_input_streamer(),
            **req_extra_args)

        if response.status_code != 200:
            raise DCOSException(
                "Input stream returned a non 200 status code")

    def _input_thread(self):
        """Reads from stdin and places a message with that data
        onto the input_queue. Expects to be running as a daemon.
        """

        # For every read of STDIN, take a line
        for chunk in iter(partial(os.read, sys.stdin.fileno(), 1024), ''):
            input_msg = {}
            input_msg['type'] = 'ATTACH_CONTAINER_INPUT'
            input_msg['attach_container_input'] = {
                'type': 'PROCESS_IO',
                'process_io': {
                    'type': 'DATA',
                    'data': {
                        'type': 'STDIN',
                        'data': base64.b64encode(chunk).decode('utf-8'),
                    }
                }
            }

            # Dump input msg to the queue for processing
            self.input_queue.put(self.encoder.encode(input_msg))

    def _output_thread(self):
        """Reads from the output_queue and writes the data
        to the appropriate stdout or stderr.
        Expects to be running as a daemon.
        """

        while True:
            # Get message from output queue
            output = self.output_queue.get()
            data = output['data']
            data = base64.b64decode(data.encode('utf-8'))

            if output['type'] == 'STDOUT':
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            elif output['type'] == 'STDERR':
                sys.stderr.buffer.write(data)
                sys.stderr.flush()

            # Close queue assuming last msg was EOF
            self.output_queue.task_done()
            self.exit_event.set()


    def _window_resizer(self):
        rows, columns = os.popen('stty size', 'r').read().split()

        window_msg = pba.TtyInfo.WindowSize(
            rows,
            columns)


def parse_pid(pid):
    """ Parse the mesos pid string,

    :param pid: pid of the form "id@ip:port"
    :type pid: str
    :returns: (id, ip, port)
    :rtype: (str, str, str)
    """

    id_, second = pid.split('@')
    ip, port = second.split(':')
    return id_, ip, port


def _merge(d, keys):
    """ Merge multiple lists from a dictionary into one iterator.
        e.g. _merge({'a': [1, 2], 'b': [3]}, ['a', 'b']) ->
             iter(1, 2, 3)

    :param d: dictionary
    :type d: dict
    :param keys: keys to merge
    :type keys: [hashable]
    :returns: iterator
    :rtype: iter
    """

    return itertools.chain(*[d[k] for k in keys])
