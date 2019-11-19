#!/usr/bin/env python
#
# Public Domain 2014-2019 MongoDB, Inc.
# Public Domain 2008-2014 WiredTiger, Inc.
#
# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

import wiredtiger, wttest
from wiredtiger import stat
from wtscenario import make_scenarios

def timestamp_str(t):
    return '%x' % t

# test_las06.py
# Verify that triggering lookaside usage does not cause a spike in memory usage
# to form an update chain from the lookaside contents.
#
# The required value should be fetched from lookaside and then passed straight
# back to the user without putting together an update chain.
#
# TODO: Uncomment the checks after the main portion of the relevant history
# project work is complete.
class test_las06(wttest.WiredTigerTestCase):
    # Force a small cache.
    conn_config = 'cache_size=50MB,statistics=(fast)'
    session_config = 'isolation=snapshot'
    key_format_values = [
        ('column_store', dict(key_format='r')),
        ('row_store', dict(key_format='i'))
    ]
    scenarios = make_scenarios(key_format_values)

    def get_stat(self, stat):
        stat_cursor = self.session.open_cursor('statistics:')
        val = stat_cursor[stat][2]
        stat_cursor.close()
        return val

    def get_non_page_image_memory_usage(self):
        return self.get_stat(stat.conn.cache_bytes_other)

    def test_las_reads(self):
        # Create a small table.
        uri = "table:test_las06"
        create_params = 'key_format={},value_format=S'.format(self.key_format)
        self.session.create(uri, create_params)

        value1 = 'a' * 500
        value2 = 'b' * 500

        # Load 5Mb of data.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(1))
        cursor = self.session.open_cursor(uri)
        self.session.begin_transaction()
        for i in range(1, 10000):
            cursor[i] = value1
        self.session.commit_transaction('commit_timestamp=' + timestamp_str(2))

        # Load another 5Mb of data with a later timestamp.
        self.session.begin_transaction()
        for i in range(1, 10000):
            cursor[i] = value2
        self.session.commit_transaction('commit_timestamp=' + timestamp_str(3))

        # Write a version of the data to disk.
        self.conn.set_timestamp('stable_timestamp=' + timestamp_str(2))
        self.session.checkpoint()

        # Check the checkpoint wrote the expected values.
        cursor2 = self.session.open_cursor(uri, None, 'checkpoint=WiredTigerCheckpoint')
        for key, value in cursor2:
            self.assertEqual(value, value1)
        cursor2.close()

        start_usage = self.get_non_page_image_memory_usage()

        # Whenever we request something out of cache of timestamp 2, we should
        # be reading it straight from lookaside without initialising a full
        # update chain of every version of the data.
        self.session.begin_transaction('read_timestamp=' + timestamp_str(2))
        for i in range(1, 10000):
            self.assertEqual(cursor[i], value1)
        self.session.rollback_transaction()

        end_usage = self.get_non_page_image_memory_usage()

        # Non-page related memory usage shouldn't spike significantly.
        #
        # Prior to this change, this type of workload would use a lot of memory
        # to recreate update lists for each page.
        #
        # This check could be more aggressive but to avoid potential flakiness,
        # lets just ensure that it hasn't doubled.
        #
        # TODO: Uncomment this once the project work is done.
        # self.assertLessEqual(end_usage, (start_usage * 2))

    def test_las_modify_reads(self):
        # Create a small table.
        uri = "table:test_las06"
        create_params = 'key_format={},value_format=S'.format(self.key_format)
        self.session.create(uri, create_params)

        # Create initial large values.
        value1 = 'a' * 500
        value2 = 'd' * 500

        # Load 5Mb of data.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(1))
        cursor = self.session.open_cursor(uri)
        self.session.begin_transaction()
        for i in range(1, 5000):
            cursor[i] = value1
        self.session.commit_transaction('commit_timestamp=' + timestamp_str(2))

        # Load a slight modification with a later timestamp.
        self.session.begin_transaction()
        for i in range(1, 5000):
            cursor.set_key(i)
            mods = [wiredtiger.Modify('B', 100, 1)]
            self.assertEqual(cursor.modify(mods), 0)
        self.session.commit_transaction('commit_timestamp=' + timestamp_str(3))

        # And another.
        self.session.begin_transaction()
        for i in range(1, 5000):
            cursor.set_key(i)
            mods = [wiredtiger.Modify('C', 200, 1)]
            self.assertEqual(cursor.modify(mods), 0)
        self.session.commit_transaction('commit_timestamp=' + timestamp_str(4))

        # Now write something completely different.
        self.session.begin_transaction()
        for i in range(1, 5000):
            cursor[i] = value2
        self.session.commit_transaction('commit_timestamp=' + timestamp_str(5))

        # Now the latest version will get written to the data file.
        self.session.checkpoint()

        expected = list(value1)
        expected[100] = 'B'
        expected[200] = 'C'
        expected = str().join(expected)

        # Whenever we request something of timestamp 4, this should be a modify
        # op. We should keep looking backwards in lookaside until we find the
        # newest whole update (timestamp 2).
        #
        # t5: value1 (full update)
        # t4: (delta) <= We're querying for t4 so we begin here.
        # t3: (delta)
        # t2: value2 (full update) <= And finish here, applying all deltas in
        #                             between on value1 to deduce value3.
        self.session.begin_transaction('read_timestamp=' + timestamp_str(4))
        for i in range(1, 5000):
            self.assertEqual(cursor[i], expected)
        self.session.rollback_transaction()

    def test_las_prepare_reads(self):
        # Create a small table.
        uri = "table:test_las06"
        create_params = 'key_format={},value_format=S'.format(self.key_format)
        self.session.create(uri, create_params)

        value1 = 'a' * 500
        value2 = 'b' * 500

        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(1))
        cursor = self.session.open_cursor(uri)
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor[i] = value1
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(2))

        # Load prepared data and leave it in a prepared state.
        prepare_session = self.conn.open_session(self.session_config)
        prepare_cursor = prepare_session.open_cursor(uri)
        prepare_session.begin_transaction()
        for i in range(1, 11):
            prepare_cursor[i] = value2
        prepare_session.prepare_transaction(
            'prepare_timestamp=' + timestamp_str(3))

        # Write some more to cause eviction of the prepared data.
        for i in range(11, 10000):
            self.session.begin_transaction()
            cursor[i] = value2
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(4))

        self.session.checkpoint()

        # Try to read every key of the prepared data again.
        # Ensure that we read the lookaside to find the prepared update and
        # return a prepare conflict as appropriate.
        self.session.begin_transaction('read_timestamp=' + timestamp_str(3))
        for i in range(1, 11):
            cursor.set_key(i)
            self.assertRaisesException(
                wiredtiger.WiredTigerError,
                lambda: cursor.search(),
                '/conflict with a prepared update/')
        self.session.rollback_transaction()

        prepare_session.commit_transaction(
            'commit_timestamp=' + timestamp_str(5) + ',durable_timestamp=' + timestamp_str(6))

        self.session.begin_transaction('read_timestamp=' + timestamp_str(5))
        for i in range(1, 11):
            self.assertEquals(value2, cursor[i])
        self.session.rollback_transaction()

    def test_las_multiple_updates(self):
        # Create a small table.
        uri = "table:test_las06"
        create_params = 'key_format={},value_format=S'.format(self.key_format)
        self.session.create(uri, create_params)

        value1 = 'a' * 500
        value2 = 'b' * 500
        value3 = 'c' * 500
        value4 = 'd' * 500

        # Load 5Mb of data.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(1))
        cursor = self.session.open_cursor(uri)
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor[i] = value1
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(2))

        # Do two different updates to the same key with the same timestamp.
        # We want to make sure that the second value is the one that is visible even after eviction.
        for i in range(1, 11):
            self.session.begin_transaction()
            cursor[i] = value2
            cursor[i] = value3
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(3))

        # Write a newer value on top.
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor[i] = value4
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(4))

        # Ensure that we see the last of the two updates that got applied.
        self.session.begin_transaction('read_timestamp=' + timestamp_str(3))
        for i in range(1, 11):
            self.assertEquals(cursor[i], value3)
        self.session.rollback_transaction()

    def test_las_multiple_modifies(self):
        # Create a small table.
        uri = "table:test_las06"
        create_params = 'key_format={},value_format=S'.format(self.key_format)
        self.session.create(uri, create_params)

        value1 = 'a' * 500
        value2 = 'b' * 500

        # Load 5Mb of data.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(1))
        cursor = self.session.open_cursor(uri)
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor[i] = value1
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(2))

        # Apply three sets of modifies.
        # They specifically need to be in separate modify calls.
        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('B', 100, 1)]), 0)
            self.assertEqual(cursor.modify([wiredtiger.Modify('C', 200, 1)]), 0)
            self.assertEqual(cursor.modify([wiredtiger.Modify('D', 300, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(3))

        expected = list(value1)
        expected[100] = 'B'
        expected[200] = 'C'
        expected[300] = 'D'
        expected = str().join(expected)

        # Write a newer value on top.
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor[i] = value2
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(4))

        # Go back and read. We should get the initial value with the 3 modifies applied on top.
        self.session.begin_transaction('read_timestamp=' + timestamp_str(3))
        for i in range(1, 11):
            self.assertEqual(cursor[i], expected)
        self.session.rollback_transaction()

    def test_las_instantiated_modify(self):
        # Create a small table.
        uri = "table:test_las06"
        create_params = 'key_format={},value_format=S'.format(self.key_format)
        self.session.create(uri, create_params)

        value1 = 'a' * 500
        value2 = 'b' * 500

        # Load 5Mb of data.
        self.conn.set_timestamp(
            'oldest_timestamp=' + timestamp_str(1) + ',stable_timestamp=' + timestamp_str(1))
        cursor = self.session.open_cursor(uri)
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor[i] = value1
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(2))

        # Apply three sets of modifies.
        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('B', 100, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(3))

        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('C', 200, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(4))

        # Since the stable timestamp is still at 1, there will be no birthmark record.
        # Lookaside instantiation should choose this update since it is the most recent.
        # We want to check that it gets converted into a standard update as appropriate.
        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('D', 300, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(5))

        # Make a bunch of updates to another table to flush everything out of cache.
        uri2 = 'table:test_las06_extra'
        self.session.create(uri2, create_params)
        cursor2 = self.session.open_cursor(uri2)
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor2[i] = value2
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(6))

        expected = list(value1)
        expected[100] = 'B'
        expected[200] = 'C'
        expected[300] = 'D'
        expected = str().join(expected)

        # Go back and read. We should get the initial value with the 3 modifies applied on top.
        self.session.begin_transaction('read_timestamp=' + timestamp_str(5))
        for i in range(1, 11):
            self.assertEqual(cursor[i], expected)
        self.session.rollback_transaction()

    def test_las_modify_birthmark_is_base_update(self):
        # Create a small table.
        uri = "table:test_las06"
        create_params = 'key_format={},value_format=S'.format(self.key_format)
        self.session.create(uri, create_params)

        value1 = 'a' * 500
        value2 = 'b' * 500

        # Load 5Mb of data.
        self.conn.set_timestamp(
            'oldest_timestamp=' + timestamp_str(1) + ',stable_timestamp=' + timestamp_str(1))

        # The base update is at timestamp 1.
        # When we lookaside evict these pages, the base update will be used as the birthmark since
        # it's the only thing behind the stable timestamp.
        cursor = self.session.open_cursor(uri)
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor[i] = value1
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(1))

        # Apply three sets of modifies.
        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('B', 100, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(2))

        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('C', 200, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(3))

        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('D', 300, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(4))

        # Make a bunch of updates to another table to flush everything out of cache.
        uri2 = 'table:test_las06_extra'
        self.session.create(uri2, create_params)
        cursor2 = self.session.open_cursor(uri2)
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor2[i] = value2
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(5))

        expected = list(value1)
        expected[100] = 'B'
        expected[200] = 'C'
        expected[300] = 'D'
        expected = str().join(expected)

        # Go back and read. We should get the initial value with the 3 modifies applied on top.
        # Ensure that we're aware that the birthmark update could be the base update.
        self.session.begin_transaction('read_timestamp=' + timestamp_str(4))
        for i in range(1, 11):
            self.assertEqual(cursor[i], expected)
        self.session.rollback_transaction()

    def test_las_rec_modify(self):
        # Create a small table.
        uri = "table:test_las06"
        create_params = 'key_format={},value_format=S'.format(self.key_format)
        self.session.create(uri, create_params)

        value1 = 'a' * 500
        value2 = 'b' * 500

        self.conn.set_timestamp(
            'oldest_timestamp=' + timestamp_str(1) + ',stable_timestamp=' + timestamp_str(1))
        cursor = self.session.open_cursor(uri)

        # Base update.
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor[i] = value1
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(2))

        # Apply three sets of modifies.
        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('B', 100, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(3))

        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('C', 200, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(4))

        # This is the one we want to be selected by the checkpoint.
        for i in range(1, 11):
            self.session.begin_transaction()
            cursor.set_key(i)
            self.assertEqual(cursor.modify([wiredtiger.Modify('D', 300, 1)]), 0)
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(5))

        # Apply another update and evict the pages with the modifies out of cache.
        for i in range(1, 10000):
            self.session.begin_transaction()
            cursor[i] = value2
            self.session.commit_transaction('commit_timestamp=' + timestamp_str(6))

        # Checkpoint such that the modifies will be selected. When we grab it from lookaside, we'll
        # need to unflatten it before using it for reconciliation.
        self.conn.set_timestamp('stable_timestamp=' + timestamp_str(5))
        self.session.checkpoint()

        expected = list(value1)
        expected[100] = 'B'
        expected[200] = 'C'
        expected[300] = 'D'
        expected = str().join(expected)

        # Check that the correct value is visible after checkpoint.
        self.session.begin_transaction('read_timestamp=' + timestamp_str(5))
        for i in range(1, 11):
            self.assertEqual(cursor[i], expected)
        self.session.rollback_transaction()

if __name__ == '__main__':
    wttest.run()
