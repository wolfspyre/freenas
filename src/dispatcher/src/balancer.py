#+
# Copyright 2014 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

import gevent
import time
import logging
import traceback
import errno
import copy
from threading import Thread
from dispatcher import validator
from dispatcher.rpc import RpcException
from gevent.queue import Queue
from gevent.event import Event
from resources import ResourceGraph, Resource
from task import TaskException, TaskStatus, TaskState


class WorkerState(object):
    IDLE = 'IDLE'
    EXECUTING = 'EXECUTING'
    WAITING = 'WAITING'


class Task(object):
    def __init__(self, dispatcher, name=None):
        self.dispatcher = dispatcher
        self.created_at = None
        self.started_at = None
        self.finished_at = None
        self.id = None
        self.name = name
        self.clazz = None
        self.args = None
        self.user = None
        self.state = TaskState.CREATED
        self.progress = None
        self.resources = []
        self.thread = None
        self.instance = None
        self.parent = None
        self.result = None
        self.ended = Event()

    def __getstate__(self):
        return {
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "name": self.name,
            "args": self.args,
            "state": self.state
        }

    def __emit_progress(self):
        self.dispatcher.dispatch_event("task.progress", {
            "id": self.id,
            "nolog": True,
            "percentage": self.progress.percentage,
            "message": self.progress.message,
            "extra": self.progress.extra
        })

    def run(self):
        self.set_state(TaskState.EXECUTING)
        try:
            result = self.instance.run(*(copy.deepcopy(self.args)))
        except BaseException, e:
            self.ended.set()
            self.set_state(TaskState.FAILED, TaskStatus(0, str(e), extra={
                "stacktrace": traceback.format_exc()
            }))
            return self.state, self.progress

        self.ended.set()
        self.result = result
        self.set_state(TaskState.FINISHED, TaskStatus(100, ''))
        return self.state, self.progress

    def start(self):
        t = Thread(target=self.run)
        t.start()
        return t

    def set_state(self, state, progress=None):
        event = {'id': self.id, 'state': state}

        if state == TaskState.EXECUTING:
            self.started_at = time.time()
            event['started_at'] = self.started_at

        if state == TaskState.FINISHED:
            self.finished_at = time.time()
            event['finished_at'] = self.finished_at
            event['result'] = self.result

        self.state = state
        self.dispatcher.dispatch_event('task.updated', event)
        self.dispatcher.datastore.update('tasks', self.id, self)

        if progress:
            self.progress = progress
            self.__emit_progress()

    def progress_watcher(self):
        while True:
            if self.ended.wait(1):
                return
            progress = self.instance.get_status()
            self.progress = progress
            self.__emit_progress()


class Balancer(object):
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.task_list = []
        self.task_queue = Queue()
        self.resource_graph = dispatcher.resource_graph
        self.queues = {}
        self.threads = []
        self.logger = logging.getLogger('Balancer')
        self.dispatcher.require_collection('tasks', 'serial', 'log')
        self.create_initial_queues()

    def create_initial_queues(self):
        self.resource_graph.add_resource(Resource('system'))

    def start(self):
        self.threads.append(gevent.spawn(self.distribution_thread))
        self.logger.info("Started")

    def schema_to_list(self, schema):
        return {
            'type': 'array',
            'items': schema,
            'minItems': sum([1 for x in schema if 'mandatory' in x and x['mandatory']]),
            'maxItems': len(schema)
        }

    def verify_schema(self, clazz, args):
        if not hasattr(clazz, 'params_schema'):
            return []

        schema = self.schema_to_list(clazz.params_schema)
        val = validator.DefaultDraft4Validator(schema, resolver=self.dispatcher.rpc.get_schema_resolver(schema))
        return list(val.iter_errors(args))

    def submit(self, name, args):
        if name not in self.dispatcher.tasks:
            self.logger.warning("Cannot submit task: unknown task type %s", name)
            raise RpcException(errno.EINVAL, "Unknown task type {0}".format(name))

        errors = self.verify_schema(self.dispatcher.tasks[name], args)
        if len(errors) > 0:
            errors = list(validator.serialize_errors(errors))
            self.logger.warning("Cannot submit task %s: schema verification failed", name)
            raise RpcException(errno.EINVAL, "Schema verification failed", extra=errors)

        task = Task(self.dispatcher, name)
        task.created_at = time.time()
        task.clazz = self.dispatcher.tasks[name]
        task.args = copy.deepcopy(args)
        task.state = TaskState.CREATED
        task.id = self.dispatcher.datastore.insert("tasks", task)
        self.task_queue.put(task)
        self.dispatcher.dispatch_event('task.created', {'id': task.id, 'type': name, 'state': task.state})
        self.logger.info("Task %d submitted (type: %s, class: %s)", task.id, name, task.clazz)
        return task.id

    def run_subtask(self, parent, name, args):
        task = Task(self.dispatcher, name)
        task.created_at = time.time()
        task.clazz = self.dispatcher.tasks[name]
        task.args = args
        task.state = TaskState.CREATED
        task.instance = task.clazz(self.dispatcher)
        task.instance.verify(*task.args)
        task.id = self.dispatcher.datastore.insert("tasks", task)
        return task.start()

    def join_subtasks(self, *tasks):
        for i in tasks:
            i.join()

    def abort(self, id):
        task = self.get_task(id)
        if not task:
            self.logger.warning("Cannot abort task: unknown task id %d", id)
            return

        success = False
        try:
            success = task.instance.abort()
        except:
            pass

        if success:
            task.ended.set()
            task.set_state(TaskState.ABORTED, TaskStatus(0, "Aborted"))

    def task_exited(self, task):
        self.resource_graph.lock()
        for i in task.resources:
            self.resource_graph.release(i)

        self.resource_graph.unlock()
        self.schedule_tasks()

    def schedule_tasks(self):
        """
        This function is called when:
        1) any new task is submitted to any of the queues
        2) any task exists

        :return:
        """

        self.resource_graph.lock()

        for task in filter(lambda t: t.state == TaskState.WAITING, self.task_list):
            if not self.resource_graph.can_acquire_all(task.resources):
                continue

            for i in task.resources:
                self.resource_graph.acquire(i)

            self.threads.append(task.start())

        self.resource_graph.unlock()

    def distribution_thread(self):
        while True:
            task = self.task_queue.get()

            try:
                self.logger.debug("Picked up task %d: %s with args %s", task.id, task.name, task.args)
                task.instance = task.clazz(self.dispatcher)
                task.resources = task.instance.verify(*task.args)

                if type(task.resources) is not list:
                    raise ValueError("verify() returned something else than resource list")

            except Exception as err:
                self.logger.warning("Cannot verify task %d: %s", task.id, err)
                task.set_state(TaskState.FAILED, TaskStatus(0, str(err)))
                continue

            task.set_state(TaskState.WAITING)
            self.task_list.append(task)
            self.schedule_tasks()
            self.logger.debug("Task %d assigned to queues %s", task.id, task.queues)

    def get_active_tasks(self):
        return filter(lambda x: x.state in (
            TaskState.CREATED,
            TaskState.WAITING,
            TaskState.EXECUTING),
            self.task_list)

    def get_tasks(self, type=None):
        if type is None:
            return self.task_list

        return filter(lambda x: x.state == type, self.task_list)

    def get_task(self, id):
        ret = filter(lambda x: x.id == id, self.task_list)
        if len(ret) > 0:
            return ret[0]
