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

import time
from helper import copy_wiredtiger_home
import unittest, wiredtiger, wttest
from wtdataset import SimpleDataSet
from test_gc01 import test_gc_base

def timestamp_str(t):
    return '%x' % t

# test_gc04.py
# Test that checkpoint must not clean the pages that are not obsolete.
class test_gc04(test_gc_base):
    conn_config = 'cache_size=50MB,log=(enabled),statistics=(all)'
    session_config = 'isolation=snapshot'

    def test_gc(self):
        nrows = 10000

        # Create a table without logging.
        uri = "table:gc04"
        ds = SimpleDataSet(
            self, uri, 0, key_format="i", value_format="S", config='log=(enabled=false)')
        ds.populate()

        # open the stats cursor
        stat_cursor = self.session.open_cursor('statistics:', None, 'statistics=(fast)')

        # Pin oldest and stable to timestamp 1.
        self.conn.set_timestamp('oldest_timestamp=' + timestamp_str(1) +
            ',stable_timestamp=' + timestamp_str(1))

        bigvalue = "aaaaa" * 100
        bigvalue2 = "ddddd" * 100
        self.large_updates(uri, bigvalue, ds, nrows, 10)
        self.large_updates(uri, bigvalue2, ds, nrows, 20)

        # Checkpoint to ensure that the history store is gets populated
        self.session.checkpoint()
        self.assertEqual(c[stat.conn.hs_gc_pages_evict][2], 0)
        self.assertEqual(c[stat.conn.hs_gc_pages_removed][2], 0)
        self.assertGreater(c[stat.conn.hs_gc_pages_visited][2], 0)

        self.large_updates(uri, bigvalue, ds, nrows, 30)

        # Checkpoint to ensure that the history store is gets populated
        self.session.checkpoint()
        self.assertEqual(c[stat.conn.hs_gc_pages_evict][2], 0)
        self.assertEqual(c[stat.conn.hs_gc_pages_removed][2], 0)
        self.assertGreater(c[stat.conn.hs_gc_pages_visited][2], 0)

        self.large_updates(uri, bigvalue2, ds, nrows, 40)

        # Checkpoint to ensure that the history store is gets populated
        self.session.checkpoint()
        self.assertEqual(c[stat.conn.hs_gc_pages_evict][2], 0)
        self.assertEqual(c[stat.conn.hs_gc_pages_removed][2], 0)
        self.assertGreater(c[stat.conn.hs_gc_pages_visited][2], 0)

        self.large_updates(uri, bigvalue, ds, nrows, 50)

        # Checkpoint to ensure that the history store is gets populated
        self.session.checkpoint()
        self.check_gc_stats()

if __name__ == '__main__':
    wttest.run()
