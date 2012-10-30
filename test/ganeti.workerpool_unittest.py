#!/usr/bin/python
#

# Copyright (C) 2008, 2009, 2010 Google Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.


"""Script for unittesting the workerpool module"""

import unittest
import threading
import time
import sys
import zlib
import random

from ganeti import workerpool
from ganeti import errors
from ganeti import utils

import testutils


class CountingContext(object):
  def __init__(self):
    self._lock = threading.Condition(threading.Lock())
    self.done = 0

  def DoneTask(self):
    self._lock.acquire()
    try:
      self.done += 1
    finally:
      self._lock.release()

  def GetDoneTasks(self):
    self._lock.acquire()
    try:
      return self.done
    finally:
      self._lock.release()

  @staticmethod
  def UpdateChecksum(current, value):
    return zlib.adler32(str(value), current)


class CountingBaseWorker(workerpool.BaseWorker):
  def RunTask(self, ctx, text):
    ctx.DoneTask()


class ChecksumContext:
  CHECKSUM_START = zlib.adler32("")

  def __init__(self):
    self.lock = threading.Condition(threading.Lock())
    self.checksum = self.CHECKSUM_START

  @staticmethod
  def UpdateChecksum(current, value):
    return zlib.adler32(str(value), current)


class ChecksumBaseWorker(workerpool.BaseWorker):
  def RunTask(self, ctx, number):
    name = "number%s" % number
    self.SetTaskName(name)

    # This assertion needs to be checked before updating the checksum. A
    # failing assertion will then cause the result to be wrong.
    assert self.getName() == ("%s/%s" % (self._worker_id, name))

    ctx.lock.acquire()
    try:
      ctx.checksum = ctx.UpdateChecksum(ctx.checksum, number)
    finally:
      ctx.lock.release()


class ListBuilderContext:
  def __init__(self):
    self.lock = threading.Lock()
    self.result = []
    self.prioresult = {}


class ListBuilderWorker(workerpool.BaseWorker):
  def RunTask(self, ctx, data):
    ctx.lock.acquire()
    try:
      ctx.result.append((self.GetCurrentPriority(), data))
      ctx.prioresult.setdefault(self.GetCurrentPriority(), []).append(data)
    finally:
      ctx.lock.release()


class DeferringTaskContext:
  def __init__(self):
    self.lock = threading.Lock()
    self.prioresult = {}
    self.samepriodefer = {}


class DeferringWorker(workerpool.BaseWorker):
  def RunTask(self, ctx, num, targetprio):
    ctx.lock.acquire()
    try:
      if num in ctx.samepriodefer:
        del ctx.samepriodefer[num]
        raise workerpool.DeferTask()

      if self.GetCurrentPriority() > targetprio:
        raise workerpool.DeferTask(priority=self.GetCurrentPriority() - 1)

      ctx.prioresult.setdefault(self.GetCurrentPriority(), set()).add(num)
    finally:
      ctx.lock.release()


class TestWorkerpool(unittest.TestCase):
  """Workerpool tests"""

  def testCounting(self):
    ctx = CountingContext()
    wp = workerpool.WorkerPool("Test", 3, CountingBaseWorker)
    try:
      self._CheckWorkerCount(wp, 3)

      for i in range(10):
        wp.AddTask((ctx, "Hello world %s" % i))

      wp.Quiesce()
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

    self.assertEquals(ctx.GetDoneTasks(), 10)

  def testNoTasks(self):
    wp = workerpool.WorkerPool("Test", 3, CountingBaseWorker)
    try:
      self._CheckWorkerCount(wp, 3)
      self._CheckNoTasks(wp)
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

  def testNoTasksQuiesce(self):
    wp = workerpool.WorkerPool("Test", 3, CountingBaseWorker)
    try:
      self._CheckWorkerCount(wp, 3)
      self._CheckNoTasks(wp)
      wp.Quiesce()
      self._CheckNoTasks(wp)
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

  def testActive(self):
    ctx = CountingContext()
    wp = workerpool.WorkerPool("TestActive", 5, CountingBaseWorker)
    try:
      self._CheckWorkerCount(wp, 5)
      self.assertTrue(wp._active)

      # Process some tasks
      for _ in range(10):
        wp.AddTask((ctx, None))

      wp.Quiesce()
      self._CheckNoTasks(wp)
      self.assertEquals(ctx.GetDoneTasks(), 10)

      # Repeat a few times
      for count in range(10):
        # Deactivate pool
        wp.SetActive(False)
        self._CheckNoTasks(wp)

        # Queue some more tasks
        for _ in range(10):
          wp.AddTask((ctx, None))

        for _ in range(5):
          # Short delays to give other threads a chance to cause breakage
          time.sleep(.01)
          wp.AddTask((ctx, "Hello world %s" % 999))
          self.assertFalse(wp._active)

        self.assertEquals(ctx.GetDoneTasks(), 10 + (count * 15))

        # Start processing again
        wp.SetActive(True)
        self.assertTrue(wp._active)

        # Wait for tasks to finish
        wp.Quiesce()
        self._CheckNoTasks(wp)
        self.assertEquals(ctx.GetDoneTasks(), 10 + (count * 15) + 15)

        self._CheckWorkerCount(wp, 5)
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

  def testChecksum(self):
    # Tests whether all tasks are run and, since we're only using a single
    # thread, whether everything is started in order.
    wp = workerpool.WorkerPool("Test", 1, ChecksumBaseWorker)
    try:
      self._CheckWorkerCount(wp, 1)

      ctx = ChecksumContext()
      checksum = ChecksumContext.CHECKSUM_START
      for i in range(1, 100):
        checksum = ChecksumContext.UpdateChecksum(checksum, i)
        wp.AddTask((ctx, i))

      wp.Quiesce()

      self._CheckNoTasks(wp)

      # Check sum
      ctx.lock.acquire()
      try:
        self.assertEqual(checksum, ctx.checksum)
      finally:
        ctx.lock.release()
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

  def testAddManyTasks(self):
    ctx = CountingContext()
    wp = workerpool.WorkerPool("Test", 3, CountingBaseWorker)
    try:
      self._CheckWorkerCount(wp, 3)

      wp.AddManyTasks([(ctx, "Hello world %s" % i, ) for i in range(10)])
      wp.AddTask((ctx, "A separate hello"))
      wp.AddTask((ctx, "Once more, hi!"))
      wp.AddManyTasks([(ctx, "Hello world %s" % i, ) for i in range(10)])

      wp.Quiesce()

      self._CheckNoTasks(wp)
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

    self.assertEquals(ctx.GetDoneTasks(), 22)

  def testManyTasksSequence(self):
    ctx = CountingContext()
    wp = workerpool.WorkerPool("Test", 3, CountingBaseWorker)
    try:
      self._CheckWorkerCount(wp, 3)
      self.assertRaises(AssertionError, wp.AddManyTasks,
                        ["Hello world %s" % i for i in range(10)])
      self.assertRaises(AssertionError, wp.AddManyTasks,
                        [i for i in range(10)])

      wp.AddManyTasks([(ctx, "Hello world %s" % i, ) for i in range(10)])
      wp.AddTask((ctx, "A separate hello"))

      wp.Quiesce()

      self._CheckNoTasks(wp)
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

    self.assertEquals(ctx.GetDoneTasks(), 11)

  def _CheckNoTasks(self, wp):
    wp._lock.acquire()
    try:
      # The task queue must be empty now
      self.assertFalse(wp._tasks)
      self.assertFalse(wp._taskdata)
    finally:
      wp._lock.release()

  def _CheckWorkerCount(self, wp, num_workers):
    wp._lock.acquire()
    try:
      self.assertEqual(len(wp._workers), num_workers)
    finally:
      wp._lock.release()

  def testPriorityChecksum(self):
    # Tests whether all tasks are run and, since we're only using a single
    # thread, whether everything is started in order and respects the priority
    wp = workerpool.WorkerPool("Test", 1, ChecksumBaseWorker)
    try:
      self._CheckWorkerCount(wp, 1)

      ctx = ChecksumContext()

      data = {}
      tasks = []
      priorities = []
      for i in range(1, 333):
        prio = i % 7
        tasks.append((ctx, i))
        priorities.append(prio)
        data.setdefault(prio, []).append(i)

      wp.AddManyTasks(tasks, priority=priorities)

      wp.Quiesce()

      self._CheckNoTasks(wp)

      # Check sum
      ctx.lock.acquire()
      try:
        checksum = ChecksumContext.CHECKSUM_START
        for priority in sorted(data.keys()):
          for i in data[priority]:
            checksum = ChecksumContext.UpdateChecksum(checksum, i)

        self.assertEqual(checksum, ctx.checksum)
      finally:
        ctx.lock.release()

      self._CheckWorkerCount(wp, 1)
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

  def testPriorityListManyTasks(self):
    # Tests whether all tasks are run and, since we're only using a single
    # thread, whether everything is started in order and respects the priority
    wp = workerpool.WorkerPool("Test", 1, ListBuilderWorker)
    try:
      self._CheckWorkerCount(wp, 1)

      ctx = ListBuilderContext()

      # Use static seed for this test
      rnd = random.Random(0)

      data = {}
      tasks = []
      priorities = []
      for i in range(1, 333):
        prio = int(rnd.random() * 10)
        tasks.append((ctx, i))
        priorities.append(prio)
        data.setdefault(prio, []).append((prio, i))

      wp.AddManyTasks(tasks, priority=priorities)

      self.assertRaises(errors.ProgrammerError, wp.AddManyTasks,
                        [("x", ), ("y", )], priority=[1] * 5)
      self.assertRaises(errors.ProgrammerError, wp.AddManyTasks,
                        [("x", ), ("y", )], task_id=[1] * 5)

      wp.Quiesce()

      self._CheckNoTasks(wp)

      # Check result
      ctx.lock.acquire()
      try:
        expresult = []
        for priority in sorted(data.keys()):
          expresult.extend(data[priority])

        self.assertEqual(expresult, ctx.result)
      finally:
        ctx.lock.release()

      self._CheckWorkerCount(wp, 1)
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

  def testPriorityListSingleTasks(self):
    # Tests whether all tasks are run and, since we're only using a single
    # thread, whether everything is started in order and respects the priority
    wp = workerpool.WorkerPool("Test", 1, ListBuilderWorker)
    try:
      self._CheckWorkerCount(wp, 1)

      ctx = ListBuilderContext()

      # Use static seed for this test
      rnd = random.Random(26279)

      data = {}
      for i in range(1, 333):
        prio = int(rnd.random() * 30)
        wp.AddTask((ctx, i), priority=prio)
        data.setdefault(prio, []).append(i)

        # Cause some distortion
        if i % 11 == 0:
          time.sleep(.001)
        if i % 41 == 0:
          wp.Quiesce()

      wp.Quiesce()

      self._CheckNoTasks(wp)

      # Check result
      ctx.lock.acquire()
      try:
        self.assertEqual(data, ctx.prioresult)
      finally:
        ctx.lock.release()

      self._CheckWorkerCount(wp, 1)
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)

  def testDeferTask(self):
    # Tests whether all tasks are run and, since we're only using a single
    # thread, whether everything is started in order and respects the priority
    wp = workerpool.WorkerPool("Test", 1, DeferringWorker)
    try:
      self._CheckWorkerCount(wp, 1)

      ctx = DeferringTaskContext()

      # Use static seed for this test
      rnd = random.Random(14921)

      data = {}
      for i in range(1, 333):
        ctx.lock.acquire()
        try:
          if i % 5 == 0:
            ctx.samepriodefer[i] = True
        finally:
          ctx.lock.release()

        prio = int(rnd.random() * 30)
        wp.AddTask((ctx, i, prio), priority=50)
        data.setdefault(prio, set()).add(i)

        # Cause some distortion
        if i % 24 == 0:
          time.sleep(.001)
        if i % 31 == 0:
          wp.Quiesce()

      wp.Quiesce()

      self._CheckNoTasks(wp)

      # Check result
      ctx.lock.acquire()
      try:
        self.assertEqual(data, ctx.prioresult)
      finally:
        ctx.lock.release()

      self._CheckWorkerCount(wp, 1)
    finally:
      wp.TerminateWorkers()
      self._CheckWorkerCount(wp, 0)


if __name__ == "__main__":
  testutils.GanetiTestProgram()
