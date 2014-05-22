# Copyright (C) 2013  Internet Systems Consortium.
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND INTERNET SYSTEMS CONSORTIUM
# DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL
# INTERNET SYSTEMS CONSORTIUM BE LIABLE FOR ANY SPECIAL, DIRECT,
# INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING
# FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT,
# NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION
# WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import unittest
import os
import re
import threading

import bundy.log
from bundy.dns import RRClass
import bundy.config
from bundy.config import parse_answer
import memmgr
from bundy.memmgr.datasrc_info import SegmentInfo
from bundy.testutils.ccsession_mock import MockModuleCCSession

# Commonly used constant for data source generation ID
TEST_GENERATION_ID = 42

class MyCCSession(MockModuleCCSession, bundy.config.ConfigData):
    def __init__(self, specfile, config_handler, command_handler):
        super().__init__()
        specfile = os.environ['BUNDY_FROM_BUILD'] + '/src/bin/memmgr/memmgr.spec'
        module_spec = bundy.config.module_spec_from_file(specfile)
        bundy.config.ConfigData.__init__(self, module_spec)
        self.add_remote_params = [] # for inspection
        self.add_remote_exception = None # to raise exception from the method
        self.rpc_call_exception = None
        self.rpc_call_params = []
        self._session = self    # internal session, to mock group_sendmsg
        self.sendmsg_params = []
        self.subscribe_notification_params = []

    def start(self):
        pass

    def add_remote_config_by_name(self, mod_name, handler):
        if self.add_remote_exception is not None:
            raise self.add_remote_exception
        self.add_remote_params.append((mod_name, handler))

    def rpc_call(self, command, group, params):
        if self.rpc_call_exception is not None:
            raise self.rpc_call_exception
        self.rpc_call_params.append((command, group, params))
        return []

    def group_sendmsg(self, cmd, group, to):
        self.sendmsg_params.append((cmd, group, to))

    def subscribe_notification(self, group, callback):
        self.subscribe_notification_params.append((group, callback))

# Test mock of SegmentInfo.  Faking many methods with hooks for easy inspection.
class MockSegmentInfo(SegmentInfo):
    def __init__(self):
        super().__init__(TEST_GENERATION_ID)
        self.events = []
        self.added_readers = []
        self.old_readers = set()
        self.ret_sync_reader = None # return value of sync_reader()
        self.raise_on_sync_reader = False
        self.loaded_result = False # return value of loaded(), tweakable

    def add_event(self, cmd):
        self.events.append(cmd)

    def start_update(self):
        return self.events[0]

    def add_reader(self, reader):
        super().add_reader(reader)
        self.added_readers.append(reader)

    def complete_validate(self, validated):
        return 'command'

    def complete_update(self, succeeded):
        return 'command'

    def get_reset_param(self, type):
        return 'test-segment-params'

    def get_old_readers(self):
        return self.old_readers

    def sync_reader(self, reader):
        if self.raise_on_sync_reader:
            raise ValueError('exception from sync_reader')
        return self.ret_sync_reader

    def _start_validate(self):
        return 'action1', 'action2'

    def loaded(self):
        return self.loaded_result

class MockMemmgr(memmgr.Memmgr):
    def __init__(self):
        super().__init__()
        self.builder_thread_created = False
        self.sent_commands = []

    def _setup_ccsession(self):
        orig_cls = bundy.config.ModuleCCSession
        bundy.config.ModuleCCSession = MyCCSession
        try:
            super()._setup_ccsession()
        finally:
            bundy.config.ModuleCCSession = orig_cls

    def _create_builder_thread(self):
        self.builder_thread_created = True

class MockDataSrcInfo:
    def __init__(self, sgmt_info):
        self.segment_info_map = {(bundy.dns.RRClass.IN, "name"): sgmt_info}
        self.gen_id = TEST_GENERATION_ID
        self.all_readers = set()
        self.canceled_readers = []

    def cancel(self, reader):
        self.canceled_readers.append(reader)
        return self.all_readers

# Defined for easier tests with DataSrcClientsMgr.reconfigure(), which
# only needs get_value() method
class MockConfigData:
    def __init__(self, data):
        self.__data = data

    def get_value(self, identifier):
        return self.__data[identifier], False

class TestMemmgr(unittest.TestCase):
    def setUp(self):
        # Some tests use this directory.  Make sure it doesn't pre-exist.
        self.__test_mapped_file_dir = \
            os.environ['BUNDY_FROM_BUILD'] + \
            '/src/bin/memmgr/tests/test_mapped_files'
        if os.path.isdir(self.__test_mapped_file_dir):
            os.rmdir(self.__test_mapped_file_dir)

        self.__mgr = MockMemmgr()
        # Fake some 'os' module functions for easier tests
        self.__orig_os_access = os.access
        self.__orig_isdir = os.path.isdir

    def tearDown(self):
        # Not all unittests cause this method to be called, so we call
        # it explicitly as it may be necessary in some cases where the
        # builder thread has been created.
        self.__mgr._shutdown_module()

        # Assert that all commands sent to the builder thread were
        # handled.
        self.assertEqual(len(self.__mgr._builder_command_queue), 0)

        # Restore faked values
        os.access = self.__orig_os_access
        os.path.isdir = self.__orig_isdir

        # If at test created a mapped-files directory, delete it.
        if os.path.isdir(self.__test_mapped_file_dir):
            os.rmdir(self.__test_mapped_file_dir)

    def __check_segment_info_update(self, sgmt_info, expected_reader,
                                    inuse_only=False):
        """Helper method for common set of checks on sent segment_info_update.

        This method assumes the caller to setup the test case with
        MockSegmentInfo and MockDataSrcInfo.

        """

        # Confirm the expected form of message has been sent.
        self.assertEqual(1, len(self.__mgr.mod_ccsession.sendmsg_params))
        (cmd, group, cc) = self.__mgr.mod_ccsession.sendmsg_params[0]
        self.assertEqual('SegmentReader', group)
        self.assertEqual(expected_reader, cc)
        command, val = bundy.config.parse_command(cmd)
        self.assertEqual('segment_info_update', command)
        expected_params = {'data-source-class': 'IN',
                           'data-source-name': 'name',
                           'segment-params': 'test-segment-params',
                           'reader': expected_reader,
                           'generation-id': 42} # genID is from MockSegmentInfo
        if inuse_only:
            expected_params['inuse-only'] = True
        self.assertEqual(expected_params, val)

        # Confirm the number of outstanding updates is managed.
        self.assertEqual(
            1, self.__mgr._segment_readers[expected_reader][sgmt_info])

    def test_init(self):
        """Check some initial conditions"""
        self.assertIsNone(self.__mgr._config_params)
        self.assertEqual(None, self.__mgr._datasrc_info)

        # Try to configure a data source clients with the manager.  This
        # should confirm the manager object is instantiated enabling in-memory
        # cache.
        cfg_data = MockConfigData(
            {"_generation_id": 1,
             "classes": {"IN": [{"type": "MasterFiles",
                                 "cache-enable": True, "params": {}}]}})
        self.__mgr._datasrc_clients_mgr.reconfigure({}, cfg_data)
        clist = \
            self.__mgr._datasrc_clients_mgr.get_client_list(RRClass.IN)
        self.assertEqual(1, len(clist.get_status()))

    def test_configure(self):
        self.__mgr._setup_ccsession()

        # Pretend specified directories exist and writable
        os.path.isdir = lambda x: True
        os.access = lambda x, y: True

        # At the initial configuration, if mapped_file_dir isn't specified,
        # the default value will be set.
        self.assertEqual((0, None),
                         parse_answer(self.__mgr._config_handler({})))
        self.assertEqual('mapped_files',
                         self.__mgr._config_params['mapped_file_dir'].
                         split('/')[-1])

        # Update the configuration.
        user_cfg = {'mapped_file_dir': '/some/path/dir'}
        self.assertEqual((0, None),
                         parse_answer(self.__mgr._config_handler(user_cfg)))
        self.assertEqual('/some/path/dir',
                         self.__mgr._config_params['mapped_file_dir'])

        # Bad update: diretory doesn't exist (we assume it really doesn't
        # exist in the tested environment).  Update won't be made.
        os.path.isdir = self.__orig_isdir # use real library
        user_cfg = {'mapped_file_dir': '/another/path/dir'}
        answer = parse_answer(self.__mgr._config_handler(user_cfg))
        self.assertEqual(1, answer[0])
        self.assertIsNotNone(re.search('not a directory', answer[1]))

    @unittest.skipIf(os.getuid() == 0, 'test cannot be run as root user')
    def test_configure_bad_permissions(self):
        self.__mgr._setup_ccsession()

        # Pretend specified directories exist and writable
        os.path.isdir = lambda x: True
        os.access = lambda x, y: True

        # Initial configuration.
        self.assertEqual((0, None),
                         parse_answer(self.__mgr._config_handler({})))

        os.path.isdir = self.__orig_isdir
        os.access = self.__orig_os_access

        # Bad update: directory exists but is not writable.
        os.mkdir(self.__test_mapped_file_dir, 0o500) # drop writable bit
        user_cfg = {'mapped_file_dir': self.__test_mapped_file_dir}
        answer = parse_answer(self.__mgr._config_handler(user_cfg))
        self.assertEqual(1, answer[0])
        self.assertIsNotNone(re.search('not writable', answer[1]))

    def test_setup_module(self):
        # _setup_module raises an exception if the initial configuration setup
        # failed
        self.assertIsNone(self.__mgr._config_params) # make sure the expetation
        self.assertRaises(bundy.server_common.bundy_server.BUNDYServerFatal,
                          self.__mgr._setup_module)

        # Set _config_params to empty config; enough for the test
        self.__mgr._config_params = {}

        # Pretend data source will have been configured correctly.  In most
        # cases below, this is okay.
        self.__mgr._datasrc_info = True

        # check what _setup_module should do:
        # 1. start the builder thread
        # 2. add data_sources remote module with expected parameters
        # 3. subscribe to zone update listener group
        # 4. subscribe to module member group
        # 5. get initial set of segment readers
        # 4 must be done before 5.  sub_notify() confirms this ordering.
        self.__mgr._setup_ccsession()
        sub_notify_params = []
        def sub_notify(g, c):
            if not sub_notify_params:
                self.assertEqual({}, self.__mgr._segment_readers)
            sub_notify_params.append((g, c))
        self.__mgr.mod_ccsession.subscribe_notification = \
            lambda g, c: sub_notify(g, c)
        self.assertFalse(self.__mgr.builder_thread_created)
        self.assertEqual([], self.__mgr.mod_ccsession.add_remote_params)
        self.assertEqual([], self.__mgr.mod_ccsession.rpc_call_params)
        self.__mgr._setup_module()
        self.assertTrue(self.__mgr.builder_thread_created)
        self.assertEqual([('ZoneUpdateListener',
                           self.__mgr._zone_update_notification),
                          ('cc_members', self.__mgr._reader_notification)],
                         sub_notify_params)
        self.assertEqual([('data_sources',
                           self.__mgr._datasrc_config_handler)],
                         self.__mgr.mod_ccsession.add_remote_params)
        self.assertEqual([('members', 'Msgq', {'group': 'SegmentReader'})],
                         self.__mgr.mod_ccsession.rpc_call_params)

        # Failure of initial data source configuration is considered fatal.
        self.__mgr._datasrc_info = None
        self.assertRaises(bundy.server_common.bundy_server.BUNDYServerFatal,
                          self.__mgr._setup_module)
        self.__mgr._datasrc_info = True # fake again for the rest of the tests

        # If data source isn't configured it's considered fatal (checking the
        # same scenario with two possible exception types)
        self.__mgr.mod_ccsession.add_remote_exception = \
            bundy.config.ModuleCCSessionError('faked exception')
        self.assertRaises(bundy.server_common.bundy_server.BUNDYServerFatal,
                          self.__mgr._setup_module)

        self.__mgr.mod_ccsession.add_remote_exception = \
            bundy.config.ModuleSpecError('faked exception')
        self.assertRaises(bundy.server_common.bundy_server.BUNDYServerFatal,
                          self.__mgr._setup_module)

        # Same for getting initial segment readers.
        self.__mgr.mod_ccsession.rpc_call_exception = bundy.config.RPCError(1,
                                                                          'e')
        self.assertRaises(bundy.server_common.bundy_server.BUNDYServerFatal,
                          self.__mgr._setup_module)

    def test_datasrc_config_handler(self):
        # Pretend to have the builder thread
        self.__mgr._builder_cv = threading.Condition()

        self.__mgr._config_params = {'mapped_file_dir': '/some/path'}

        # A simple (boring) case with real class implementations.  This
        # confirms the methods are called as expected.
        cfg_data = MockConfigData(
            {"_generation_id": 1,
             "classes": {"IN": [{"type": "MasterFiles",
                                 "cache-enable": True, "params": {}}]}})
        self.__init_called = None
        def mock_init_segments(param):
            self.__init_called = param
        self.__mgr._init_segments = mock_init_segments
        self.__mgr._datasrc_config_handler({}, cfg_data)
        self.assertIsNotNone(self.__mgr._datasrc_info)
        self.assertEqual(1, self.__mgr._datasrc_info.gen_id)
        self.assertEqual(self.__init_called, self.__mgr._datasrc_info)
        self.assertEqual([], self.__mgr._builder_command_queue)

        # Below we're using a mock DataSrcClientMgr for easier tests
        class MockDataSrcClientMgr:
            def __init__(self, status_list, raise_on_reconfig=False):
                self.__status_list = status_list
                self.__raise_on_reconfig = raise_on_reconfig

            def reconfigure(self, new_config, config_data):
                if self.__raise_on_reconfig:
                    raise bundy.server_common.datasrc_clients_mgr.ConfigError(
                        'test error')
                # otherwise do nothing

            def get_clients_map(self):
                return 42, {RRClass.IN: self}

            def get_status(self): # mocking get_clients_map()[1].get_status()
                return self.__status_list

        # This confirms memmgr's config is passed and handled correctly.
        # From memmgr's point of view it should be enough we have an object
        # in segment_info_map.  Note also that the new DataSrcInfo is appended
        # to the list
        old_datasrc_info = self.__mgr._datasrc_info
        self.__mgr._datasrc_clients_mgr = \
            MockDataSrcClientMgr([('sqlite3', 'mapped', None)])
        self.__mgr._datasrc_config_handler(None, None) # params don't matter
        self.assertIsNotNone(self.__mgr._datasrc_info)
        self.assertEqual(1, len(self.__mgr._old_datasrc_info))
        self.assertEqual(self.__init_called, self.__mgr._datasrc_info)
        self.assertIsNotNone(
            self.__mgr._datasrc_info.segment_info_map[(RRClass.IN, 'sqlite3')])
        # The old info should be moved to the 'old' set, and a cancel command
        # should have been sent to the builder.
        self.assertSetEqual({old_datasrc_info},
                            set(self.__mgr._old_datasrc_info.values()))
        self.assertEqual([('cancel', old_datasrc_info)],
                         self.__mgr._builder_command_queue)
        del self.__mgr._builder_command_queue[:] # for tearDown

        # Emulate the case reconfigure() fails.  Exception isn't propagated,
        # but the status doesn't change.
        self.__mgr._datasrc_clients_mgr = MockDataSrcClientMgr(None, True)
        self.__mgr._datasrc_config_handler(None, None)
        self.assertIsNotNone(self.__mgr._datasrc_info)
        self.assertEqual(1, len(self.__mgr._old_datasrc_info))

    def test_init_segments(self):
        """Test the initialization of segments.

        just load everything found in there.

        """
        # Fake a lot of things. These are objects hard to set up, so this is
        # easier.
        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)

        # Pretend to have the builder thread
        self.__mgr._builder_cv = threading.Condition()

        # Run the initialization
        self.__mgr._segment_readers = {'reader1': {}, 'reader2': {}}
        self.__mgr._init_segments(dsrc_info)

        # all readers should have been added to the segment(s).
        added_readers = sgmt_info.added_readers
        added_readers.sort()
        self.assertEqual(['reader1', 'reader2'], added_readers)

        # Check the first command sent to the builder thread.
        self.assertEqual(self.__mgr._builder_command_queue,
                         [('validate', dsrc_info, bundy.dns.RRClass.IN, 'name',
                          'action1')])

        # Check pending events pushed into the segment info
        self.assertEqual(len(sgmt_info.events), 2)
        self.assertEqual(sgmt_info.events[0],
                         ('validate', dsrc_info, bundy.dns.RRClass.IN, 'name',
                          'action2'))
        self.assertEqual(sgmt_info.events[1],
                         ('load', None, dsrc_info, bundy.dns.RRClass.IN,
                          'name'))
        del self.__mgr._builder_command_queue[:]

    # Check the handling of 'update/validate-completed' notification from
    # the builder.
    def __check_notify_from_builder(self, notif_name, notif_ref, dsrc_info,
                                    sgmt_info, commands):
        sgmt_info.complete_update = lambda x: 'command'
        sgmt_info.complete_validate = lambda x: 'command'
        del commands[:]
        del self.__mgr.mod_ccsession.sendmsg_params[:]
        sgmt_info.old_readers.clear()

        notif_ref.append((notif_name, dsrc_info, bundy.dns.RRClass.IN,
                          'name', True))
        # Wake up the main thread and let it process the notifications
        self.__mgr._notify_from_builder()
        # All notifications are now eaten
        self.assertEqual([], notif_ref)
        self.assertEqual(['command'], commands)
        del commands[:]
        # no message to readers
        self.assertEqual(0, len(self.__mgr.mod_ccsession.sendmsg_params))

        # The new command is sent
        # Once again the same, but with the last command - nothing new pushed,
        # but a notification should be sent to readers
        sgmt_info.old_readers.add('reader1')
        self.__mgr._segment_readers['reader1'] = {}
        sgmt_info.complete_update = lambda x: None
        sgmt_info.complete_validate = lambda x: None
        notif_ref.append((notif_name, dsrc_info, bundy.dns.RRClass.IN,
                          'name', True))
        self.__mgr._notify_from_builder()
        self.assertEqual([], notif_ref)
        self.assertEqual([], commands)
        self.assertEqual(1, len(self.__mgr.mod_ccsession.sendmsg_params))
        (cmd, group, cc) = self.__mgr.mod_ccsession.sendmsg_params[0]
        self.__check_segment_info_update(sgmt_info, 'reader1',
                                         notif_name == 'validate-completed')

    # Check the handling of cancel-command notification from builder.
    def __check_cancel_completed_from_builder(self, notif_ref, dsrc_info):
        # If there's no reader, the data source info is immediately cleaned up.
        notif_ref.append(('cancel-completed', dsrc_info))
        self.__mgr._old_datasrc_info[TEST_GENERATION_ID] = dsrc_info
        self.__mgr._notify_from_builder()
        self.assertFalse(TEST_GENERATION_ID in self.__mgr._old_datasrc_info)

        # Otherwise, 'release_segments' command will be sent to the reader.
        self.__mgr.mod_ccsession.sendmsg_params.clear()
        self.__mgr._old_datasrc_info[TEST_GENERATION_ID] = dsrc_info
        dsrc_info.all_readers = set({'reader1', 'reader2'})
        notif_ref.append(('cancel-completed', dsrc_info))
        self.__mgr._notify_from_builder()
        self.assertEqual(2, len(self.__mgr.mod_ccsession.sendmsg_params))
        cmds = []
        groups = set()
        recipients = set()
        [(cmds.append(cmd), groups.add(g), recipients.add(cc)) for cmd, g, cc in
         self.__mgr.mod_ccsession.sendmsg_params]
        self.assertEqual(set({'reader1', 'reader2'}), recipients)
        self.assertEqual(set({'SegmentReader'}), groups)
        command, val = bundy.config.parse_command(cmds[0])
        self.assertEqual('release_segments', command)
        self.assertEqual(TEST_GENERATION_ID, val['generation-id'])

        # not yet cleaned up
        self.assertEqual(dsrc_info,
                         self.__mgr._old_datasrc_info[TEST_GENERATION_ID])

    def test_notify_from_builder(self):
        """
        Check the notify from builder thing eats the notifications and
        handles them.
        """
        # Some mocks
        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)
        class Sock:
            def recv(self, size):
                pass
        self.__mgr._master_sock = Sock()
        commands = []
        def mock_cmd_to_builder(cmd):
            commands.append(cmd)
        self.__mgr._cmd_to_builder = mock_cmd_to_builder
        self.__mgr._mod_cc = MyCCSession(None, None, None) # fake mod_ccsession

        self.__mgr._builder_lock = threading.Lock()
        # Extract the reference for the queue. We get a copy of the reference
        # to check it is cleared, not a new empty one installed
        notif_ref = self.__mgr._builder_response_queue

        self.__check_notify_from_builder('load-completed', notif_ref, dsrc_info,
                                         sgmt_info, commands)
        self.__check_notify_from_builder('validate-completed', notif_ref,
                                         dsrc_info, sgmt_info, commands)
        self.__check_cancel_completed_from_builder(notif_ref, dsrc_info)

        # This is invalid (unhandled) notification name
        notif_ref.append(('unhandled',))
        self.assertRaises(ValueError, self.__mgr._notify_from_builder)
        self.assertEqual([], notif_ref)

    def test_send_to_builder(self):
        """
        Send command to the builder test.
        """
        self.__mgr._builder_cv = threading.Condition()
        self.__mgr._cmd_to_builder(('test',))
        self.assertEqual([('test',)], self.__mgr._builder_command_queue)
        del self.__mgr._builder_command_queue[:]

    def test_mod_command_handler(self):
        # unknown name of command will be rejected.  known cases are tested
        # separately.
        ans = self.__mgr._mod_command_handler('unknown', {})
        self.assertEqual(1, parse_answer(ans)[0])

    def test_segment_info_update_ack(self):
        "Normal case of segment_info_update_ack command"

        commands = []
        self.__mgr._cmd_to_builder = lambda cmd: commands.append(cmd)

        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)
        self.__mgr._datasrc_info = dsrc_info

        # If sync_reader() returns None, no command should be sent to
        # the segment builder.  We emulate the situation where there are
        # two outstanding segment_info_update.  The counter is updated
        # for each callto _mod_command_handler, and sync_reader() is called
        # if and only if all outstanding segment_info_update's are responded.
        sgmt_readers = self.__mgr._segment_readers
        sgmt_readers['reader0'] = {sgmt_info: 2}
        for i in (0, 1):
            self.assertEqual(False, self.__mgr._mod_command_handler(
                'segment_info_update_ack', {'data-source-class': 'IN',
                                            'data-source-name': 'name',
                                            'generation-id': TEST_GENERATION_ID,
                                            'reader': 'reader0'}))
            self.assertEqual([], commands)
            if i == 0:
                self.assertEqual(1, sgmt_readers['reader0'][sgmt_info])
            else:
                self.assertFalse(sgmt_info in sgmt_readers['reader0'])

        # If sync_returns() something, it's passed to the builder.
        self.__mgr._segment_readers['reader0'] = {sgmt_info: 2}
        for i in (0, 1):
            sgmt_info.ret_sync_reader = 'cmd'
            self.assertEqual(False, self.__mgr._mod_command_handler(
                'segment_info_update_ack', {'data-source-class': 'IN',
                                            'data-source-name': 'name',
                                            'generation-id': TEST_GENERATION_ID,
                                            'reader': 'reader0'}))
            if i == 0:
                self.assertEqual(1, sgmt_readers['reader0'][sgmt_info])
                self.assertEqual([], commands)
            else:
                self.assertFalse(sgmt_info in sgmt_readers['reader0'])
                self.assertEqual(['cmd'], commands)

    def test_bad_segment_info_update_ack(self):
        "Check various invalid cases of segment_info_update_ack command"

        # there's no datasrc info
        self.assertIsNone(self.__mgr._mod_command_handler(
            'segment_info_update_ack', {}))
        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)
        self.__mgr._datasrc_info = dsrc_info
        # missing necesary keys or invalid values
        self.assertIsNone(self.__mgr._mod_command_handler(
            'segment_info_update_ack', {}))
        self.assertIsNone(self.__mgr._mod_command_handler(
            'segment_info_update_ack', {'data-source-class': 'badclass',
                                        'generation-id': TEST_GENERATION_ID}))
        self.assertIsNone(self.__mgr._mod_command_handler(
            'segment_info_update_ack', {'data-source-class': 'IN',
                                        'generation-id': TEST_GENERATION_ID}))
        self.assertIsNone(self.__mgr._mod_command_handler(
            'segment_info_update_ack', {'data-source-class': 'IN',
                                        'data-source-name': 'noname',
                                        'generation-id': TEST_GENERATION_ID}))
        self.assertIsNone(self.__mgr._mod_command_handler(
            'segment_info_update_ack', {'data-source-class': 'IN',
                                        'data-source-name': 'name',
                                        'generation-id': TEST_GENERATION_ID}))

        # getting segment_info_update_ack for an older generation is possible,
        # if the data source is reconfigured while waiting for the response.
        # Getting it for a newer generation shouldn't happen, but the end result
        # is the same (ignore the ack), so we don't differentiate them.
        self.assertFalse(self.__mgr._mod_command_handler(
            'segment_info_update_ack', {'data-source-class': 'IN',
                                        'data-source-name': 'name',
                                        'generation-id': TEST_GENERATION_ID - 1,
                                        'reader': 'reader0'}))
        self.assertFalse(self.__mgr._mod_command_handler(
            'segment_info_update_ack', {'data-source-class': 'IN',
                                        'data-source-name': 'name',
                                        'generation-id': TEST_GENERATION_ID + 1,
                                        'reader': 'reader0'}))

        # not necessarily invalid, but less common case: reader has been
        # removed by the time of handling update_ack.
        self.assertEqual(False,
                         self.__mgr._mod_command_handler(
                             'segment_info_update_ack',
                             {'data-source-class': 'IN',
                              'data-source-name': 'name',
                              'generation-id': TEST_GENERATION_ID,
                              'reader': 'reader0'}))

        # exception from sync_readers
        sgmt_info.raise_on_sync_reader = True
        self.__mgr._segment_readers['reader0'] = {sgmt_info: 1}
        self.assertIsNone(self.__mgr._mod_command_handler(
            'segment_info_update_ack', {'data-source-class': 'IN',
                                        'data-source-name': 'name',
                                        'generation-id': TEST_GENERATION_ID,
                                        'reader': 'reader0'}))

    def test_release_segments_ack(self):
        "Test handling of release_segments_ack command."

        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)
        self.__mgr._datasrc_info = dsrc_info
        self.__mgr._old_datasrc_info[TEST_GENERATION_ID] = dsrc_info

        # A valid release_segments_ack always cause canceling of the sender
        # reader for the corresponding data source info.  If there's still
        # a reader, that info should still stay in _old_datasrc_info.
        dsrc_info.all_readers = set({'more-reader'})
        self.__mgr._mod_command_handler('release_segments_ack',
                                        {'generation-id': TEST_GENERATION_ID,
                                         'reader': 'reader0'})
        self.assertEqual(['reader0'], dsrc_info.canceled_readers)
        self.assertEqual(dsrc_info,
                         self.__mgr._old_datasrc_info[TEST_GENERATION_ID])

        # If there's no more reader, that generation of data source info should
        # be cleaned up.
        dsrc_info.canceled_readers.clear()
        dsrc_info.all_readers.clear()
        self.__mgr._mod_command_handler('release_segments_ack',
                                        {'generation-id': TEST_GENERATION_ID,
                                         'reader': 'reader1'})
        self.assertEqual(['reader1'], dsrc_info.canceled_readers)
        self.assertFalse(TEST_GENERATION_ID in self.__mgr._old_datasrc_info)

        # Test invalid cases: these shouldn't cause disruption, and data
        # source info should be intact.
        dsrc_info.canceled_readers.clear()
        dsrc_info.all_readers.clear()
        self.__mgr._old_datasrc_info[TEST_GENERATION_ID] = dsrc_info
        # missing generation-id
        self.__mgr._mod_command_handler('release_segments_ack',
                                        {'reader': 'reader2'})
        # missing 'reader'
        self.__mgr._mod_command_handler('release_segments_ack',
                                        {'generation-id': TEST_GENERATION_ID})
        # generation-id mismatch
        self.__mgr._mod_command_handler('release_segments_ack',
                                        {'generation-id': 1,
                                         'reader': 'reader1'})
        # cancel() raises an exception
        def raiser(): raise ValueError('test error')
        dsrc_info.cancel = lambda x: raiser()
        self.__mgr._mod_command_handler('release_segments_ack',
                                        {'generation-id': TEST_GENERATION_ID,
                                         'reader': 'reader1'})

        # no call to cancel() except for the faked one
        self.assertFalse(dsrc_info.canceled_readers)
        self.assertEqual(dsrc_info,
                         self.__mgr._old_datasrc_info[TEST_GENERATION_ID])

    def __check_load_or_update_zone(self, cmd, handler):
        "Common checks for load or update zone callbacks."

        commands = []
        self.__mgr._cmd_to_builder = lambda cmd: commands.append(cmd)

        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)
        self.__mgr._datasrc_info = dsrc_info

        # Expected builder event for the loadzone parameters
        expected_event = ('load', bundy.dns.Name('zone'), dsrc_info,
                          bundy.dns.RRClass('IN'), 'name')

        # If start_update() returns an event, it's passed to the builder.
        cmd_args = {'datasource': 'name', 'class': 'IN', 'origin': 'zone'}
        if cmd == 'zone_updated':
            cmd_args['generation-id'] = TEST_GENERATION_ID
        ans = handler(cmd, cmd_args)
        if cmd == 'loadzone':
            self.assertEqual(0, parse_answer(ans)[0])
        self.assertEqual([expected_event], sgmt_info.events)
        self.assertEqual([expected_event], commands)

        # If start_update() returns None, the event is only stored in the
        # segment info.
        sgmt_info.start_update = lambda: None
        ans = handler(cmd, cmd_args)
        if cmd == 'loadzone':
            self.assertEqual(0, parse_answer(ans)[0])
        self.assertEqual([expected_event, expected_event], sgmt_info.events)
        self.assertEqual([expected_event], commands)

    def test_loadzone(self):
        "Normal case of loadzone command"

        self.__check_load_or_update_zone('loadzone',
                                         self.__mgr._mod_command_handler)

    def test_bad_loadzone(self):
        "Check various invalid cases of loadzone command"

        # there's no datasrc info
        self.assertEqual(1, parse_answer(self.__mgr._mod_command_handler(
            'loadzone', {}))[0])

        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)
        self.__mgr._datasrc_info = dsrc_info

        # missing necesary keys or invalid values
        self.assertEqual(1, parse_answer(self.__mgr._mod_command_handler(
            'loadzone', {}))[0])
        self.assertEqual(1, parse_answer(self.__mgr._mod_command_handler(
            'loadzone', {'class': 'badclass', 'datasource': 'name',
                         'origin': 'zone'}))[0])
        self.assertEqual(1, parse_answer(self.__mgr._mod_command_handler(
            'loadzone', {'class': 'IN', 'datasource': 'name',
                         'origin': 'bad..name'}))[0])
        self.assertEqual(1, parse_answer(self.__mgr._mod_command_handler(
            'loadzone', {'class': 'IN', 'datasource': 'noname',
                         'origin': 'zone'}))[0])

    def test_bad_zone_updated(self):
        """Check various invalid/rare cases of 'zone_updated' command.

        In either case, no new event for the segment should happen.

        """
        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)
        self.__mgr._datasrc_info = dsrc_info
        sgmt_info.start_update = lambda: None

        # missing generation ID
        self.__mgr._zone_update_notification('zone_updated',
                                       {'class': 'IN', 'datasource': 'name',
                                        'origin': 'zone'})
        self.assertFalse(sgmt_info.events)

        # generation ID mismatch
        self.__mgr._zone_update_notification('zone_updated',
                                       {'class': 'IN', 'datasource': 'name',
                                        'origin': 'zone',
                                        'generation-id':
                                        TEST_GENERATION_ID + 1})
        self.assertFalse(sgmt_info.events)

    def test_reader_notification(self):
        "Test module membership notification callback."

        # unrelated notifications should be ignored and shouldn't cause
        # disruption.
        self.__mgr._reader_notification('connected', {'client': 'foo'})
        self.__mgr._reader_notification('disconnected', {'client': 'foo'})
        self.__mgr._reader_notification('subscribed', {'group': 'TestGroup'})
        self.__mgr._reader_notification('unsubscribed', {'group': 'TestGroup'})

        # necessary setup for the memmgr
        self.__mgr._mod_cc = MyCCSession(None, None, None) # fake mod_ccsession
        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)
        self.__mgr._datasrc_info = dsrc_info

        # basic case of new subscriber.  the reader should be added to the
        # segment info, and info_update should be sent to the reader.
        sgmt_info.loaded_result = True
        self.__mgr._reader_notification('subscribed',
                                        {'client': 'foo',
                                         'group': 'SegmentReader'})
        self.assertEqual(['foo'], sgmt_info.added_readers)
        self.__check_segment_info_update(sgmt_info, 'foo')

        # duplicate subscribe notification.  rare, but possible.  basically
        # no-op.
        self.__mgr._reader_notification('subscribed',
                                        {'client': 'foo',
                                         'group': 'SegmentReader'})
        self.assertEqual(['foo'], sgmt_info.added_readers) # should be no change

        # if a reader segment isn't yet available, no info_update will be sent.
        self.__mgr.mod_ccsession.sendmsg_params = []
        sgmt_info.loaded_result = False
        self.__mgr._reader_notification('subscribed',
                                        {'client': 'bar',
                                         'group': 'SegmentReader'})
        self.assertEqual([], self.__mgr.mod_ccsession.sendmsg_params)

        # basic case of unsubscription of a reader.  reader will be removed
        # from memmgr's list.  unless remove_reader() returns a new command,
        # no new command will be sent to the builder.
        commands = []
        self.__mgr._cmd_to_builder = lambda cmd: commands.append(cmd)
        self.__mgr._reader_notification('unsubscribed',
                                        {'client': 'foo',
                                         'group': 'SegmentReader'})
        self.assertFalse('foo' in self.__mgr._segment_readers)
        self.assertEqual([], commands)

        # if remove_reader() returns a new command, it will be sent to builder.
        sgmt_info.remove_reader = lambda r: 'some command'
        self.__mgr._reader_notification('unsubscribed',
                                        {'client': 'bar',
                                         'group': 'SegmentReader'})
        self.assertFalse('bar' in self.__mgr._segment_readers)
        self.assertEqual(['some command'], commands)

        # if specified reader doesn't exist in the memmgr's list, it's ignored
        # although it's an unexpected event.
        self.__mgr._reader_notification('unsubscribed',
                                        {'client': 'baz',
                                         'group': 'SegmentReader'})

    def test_reader_unsubscribe_and_cancel(self):
        # Specific tests in the case of unsubscribed reader that causes
        # canceling deprecated data source info.
        sgmt_info = MockSegmentInfo()
        dsrc_info = MockDataSrcInfo(sgmt_info)
        self.__mgr._datasrc_info = dsrc_info
        self.__mgr._segment_readers['reader0'] = {}

        # Set up some "old" data source info.  One of them would return a
        # non-empty set for cancel() and should survive.
        for i in range(0, 3):
            info = MockDataSrcInfo(sgmt_info)
            info.gen_id = i
            self.__mgr._old_datasrc_info[i] = info
        self.__mgr._old_datasrc_info[2].all_readers = set({'reader'})

        # Send unsubsribed notification, and check completed info is cleaned up
        self.__mgr._reader_notification('unsubscribed',
                                        {'client': 'reader0',
                                         'group': 'SegmentReader'})
        self.assertEqual({2: self.__mgr._old_datasrc_info[2]},
                         self.__mgr._old_datasrc_info)

    def test_zone_update_notification(self):
        "Test zone update notification callback."

        # unrelated notifications should be ignored and shouldn't cause
        # disruption.
        self.__mgr._zone_update_notification('unknown', {})

        # Perform the same tests for the 'loadzone' command.
        self.__check_load_or_update_zone('zone_updated',
                                         self.__mgr._zone_update_notification)

if __name__== "__main__":
    bundy.log.resetUnitTestRootLogger()
    unittest.main()
