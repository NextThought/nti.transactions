#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function, absolute_import, division

# pylint:disable=too-many-public-methods

import sys
import unittest

from hamcrest import assert_that
from hamcrest import is_
from hamcrest import calling
from hamcrest import raises
from hamcrest import has_property
from hamcrest import none

import fudge

from nti.testing.matchers import is_true
from nti.testing.matchers import is_false

from ..interfaces import CommitFailedError
from ..interfaces import AbortFailedError
from ..interfaces import ForeignTransactionError
from ..interfaces import TransactionLifecycleError

from ..loop import _do_commit
from ..loop import TransactionLoop

import transaction
from transaction.interfaces import TransientError
from transaction.interfaces import NoTransaction
from transaction.interfaces import AlreadyInTransaction


class TestCommit(unittest.TestCase):
    class RaisingCommit(object):
        def __init__(self, t=Exception):
            self.t = t

        def nti_commit(self):
            if self.t:
                raise self.t

    def test_commit_raises_type_error_raises_commit_failed(self):
        assert_that(calling(_do_commit).with_args(self.RaisingCommit(TypeError),
                                                  '', 0),
                    raises(CommitFailedError))

    def test_commit_raises_type_error_raises_commit_failed_good_message(self):
        assert_that(calling(_do_commit).with_args(
            self.RaisingCommit(TypeError("A custom message")),
            '', 0),
                    raises(CommitFailedError, "A custom message"))


    @fudge.patch('nti.transactions.transactions.logger.exception')
    def test_commit_raises_assertion_error(self, fake_logger):
        fake_logger.expects_call()

        assert_that(calling(_do_commit).with_args(self.RaisingCommit(AssertionError),
                                                  '', 0),
                    raises(AssertionError))

    @fudge.patch('nti.transactions.transactions.logger.exception')
    def test_commit_raises_value_error(self, fake_logger):
        fake_logger.expects_call()

        assert_that(calling(_do_commit).with_args(self.RaisingCommit(ValueError),
                                                  '', 0),
                    raises(ValueError))

    @fudge.patch('nti.transactions.transactions.logger.exception')
    def test_commit_raises_custom_error(self, fake_logger):
        fake_logger.expects_call()

        class MyException(Exception):
            pass

        try:
            raise MyException()
        except MyException:
            assert_that(calling(_do_commit).with_args(self.RaisingCommit(ValueError),
                                                      '', 0),
                        raises(MyException))

    @fudge.patch('nti.transactions.loop.logger.warn')
    def test_commit_clean_but_long(self, fake_logger):
        fake_logger.expects_call()
        _do_commit(self.RaisingCommit(None), '', -1)

class TestLoop(unittest.TestCase):

    def setUp(self):
        try:
            transaction.abort()
        except NoTransaction:
            pass

    def test_trivial(self):
        result = TransactionLoop(lambda a: a, retries=1, long_commit_duration=1, sleep=1)(1)
        assert_that(result, is_(1))

    def test_explicit(self):
        assert_that(transaction.manager, has_property('explicit', is_false()))

        def handler():
            assert_that(transaction.manager, has_property('explicit', is_true()))
            return 42

        result = TransactionLoop(handler)()
        assert_that(result, is_(42))

    def test_explicit_begin(self):
        def handler():
            transaction.begin()

        assert_that(calling(TransactionLoop(handler)), raises(AlreadyInTransaction))

    def test_explicit_begin_after_commit(self):
        # We change the current transaction out and then still manage to raise
        # AlreadyInTransaction
        def handler():
            transaction.abort()
            transaction.begin()
            transaction.begin()

        assert_that(calling(TransactionLoop(handler)), raises(AlreadyInTransaction))


    def test_explicit_end(self):
        def handler():
            transaction.abort()

        assert_that(calling(TransactionLoop(handler)), raises(TransactionLifecycleError))

    def test_explicit_foreign(self):
        def handler():
            transaction.abort()
            transaction.begin()

        assert_that(calling(TransactionLoop(handler)), raises(ForeignTransactionError))

    def test_explicit_foreign_abort_fails(self):
        def bad_abort():
            raise Exception("Bad abort")

        def handler():
            transaction.abort()
            tx = transaction.begin()
            tx.abort = tx.nti_abort = bad_abort

        assert_that(calling(TransactionLoop(handler)), raises(ForeignTransactionError))
        assert_that(transaction.manager.manager, has_property('_txn', is_(none())))

    def test_setup_teardown(self):

        class Loop(TransactionLoop):
            setupcalled = teardowncalled = False
            def setUp(self):
                assert_that(transaction.manager, has_property('explicit', is_true()))
                self.setupcalled = True
            def tearDown(self):
                self.teardowncalled = True

        def handler():
            raise Exception

        loop = Loop(handler)
        assert_that(calling(loop), raises(Exception))

        assert_that(loop, has_property('setupcalled', is_true()))
        assert_that(loop, has_property('teardowncalled', is_true()))

    def test_retriable(self, loop_class=TransactionLoop, exc_type=TransientError,
                       raise_count=1, loop_args=(), loop_kwargs=None):

        calls = []
        def handler():
            # exc_info should be clear on entry.
            assert_that(sys.exc_info(), is_((None, None, None)))
            if len(calls) < raise_count:
                calls.append(1)
                raise exc_type(calls)
            return "hi"

        loop = loop_class(handler, *loop_args, **(loop_kwargs or {}))
        result = loop()
        assert_that(result, is_("hi"))
        assert_that(calls, is_([1] * raise_count))
        return loop

    def test_custom_retriable(self):
        class Loop(TransactionLoop):
            _retryable_errors = ((Exception, None),)

        self.test_retriable(Loop, AssertionError)

    def test_retriable_gives_up(self):
        def handler():
            raise TransientError()
        loop = TransactionLoop(handler, sleep=0.01, retries=1)
        assert_that(calling(loop), raises(TransientError))

    def test_non_retryable(self):
        class MyError(Exception):
            pass
        def handler():
            raise MyError()
        loop = TransactionLoop(handler, sleep=0.01, retries=100000000)
        assert_that(calling(loop), raises(MyError))

    def test_isRetryableError_exception(self):
        # If the transaction.isRetryableError() raises, for some reason,
        # we still process our list
        class MyError(object):
            pass
        class Loop(TransactionLoop):
            _retryable_errors = ((MyError, None),)

        loop = Loop(None)
        loop._retryable(None, (None, MyError(), None))

    def test_retryable_backoff(self):
        class NotRandom(object):
            def randint(self, _floor, ceiling):
                return ceiling

        class Loop(TransactionLoop):
            def __init__(self, *args, **kwargs):
                TransactionLoop.__init__(self, *args, **kwargs)
                self.times = []
                self.random = NotRandom()
                self._sleep = self.times.append

        # By default, it is not called.
        loop = self.test_retriable(Loop, raise_count=5)
        assert_that(loop, has_property('times', []))

        # Setting a delay calls it
        loop = self.test_retriable(Loop, raise_count=5, loop_kwargs={'sleep': 0.1})
        # The ceiling arguments are 2**attempt - 1, so
        # 1, 3, 7, 15, 31, and sleep times are
        # 0.1, 0.3, 0.7, 1.5, 3,1
        times = [(2 ** x - 1) * 0.1 for x in range(1, 6)]
        assert_that(loop, has_property('times',
                                       times))


    @fudge.patch('transaction._manager.TransactionManager.begin',
                 'transaction._manager.TransactionManager.get')
    def test_note(self, fake_begin, fake_get):
        fake_tx = fudge.Fake()
        (fake_tx
         .expects('note').with_args(u'Hi')
         .expects('nti_abort')
         .provides('isDoomed').returns(True))
        fake_begin.expects_call().returns(fake_tx)
        fake_get.expects_call().returns(fake_tx)
        class Loop(TransactionLoop):
            def describe_transaction(self, *args, **kwargs):
                return u"Hi"

        result = Loop(lambda: 42)()
        assert_that(result, is_(42))


    @fudge.patch('transaction._manager.TransactionManager.begin',
                 'transaction._manager.TransactionManager.get')
    def test_abort_no_side_effect(self, fake_begin, fake_get):
        fake_tx = fudge.Fake()
        fake_tx.expects('nti_abort')

        fake_begin.expects_call().returns(fake_tx)
        fake_get.expects_call().returns(fake_tx)


        class Loop(TransactionLoop):
            side_effect_free = True

        result = Loop(lambda: 42)()
        assert_that(result, is_(42))

    @fudge.patch('transaction._transaction.Transaction.nti_abort')
    def test_abort_doomed(self, fake_abort):
        fake_abort.expects_call()

        def handler():
            assert_that(transaction.manager.explicit, is_true())
            transaction.get().doom()
            return 42

        result = TransactionLoop(handler)()
        assert_that(result, is_(42))

    @fudge.patch('transaction._manager.TransactionManager.begin',
                 'transaction._manager.TransactionManager.get')
    def test_abort_veto(self, fake_begin, fake_get):
        fake_tx = fudge.Fake()
        fake_tx.expects('nti_abort')
        fake_tx.provides('isDoomed').returns(False)

        fake_begin.expects_call().returns(fake_tx)
        fake_get.expects_call().returns(fake_tx)

        class Loop(TransactionLoop):
            def should_veto_commit(self, result, *args, **kwargs):
                assert_that(result, is_(42))
                return True

        result = Loop(lambda: 42)()
        assert_that(result, is_(42))

    @fudge.patch('transaction._manager.TransactionManager.begin')
    def test_abort_systemexit(self, fake_begin):
        fake_tx = fudge.Fake()
        fake_tx.expects('abort').raises(ValueError)
        fake_tx.provides('isDoomed').returns(False)

        fake_begin.expects_call().returns(fake_tx)

        def handler():
            raise SystemExit()

        loop = TransactionLoop(handler)
        try:
            loop()
            self.fail("Should raise SystemExit")
        except SystemExit:
            pass

    @fudge.patch('transaction._manager.TransactionManager.begin',
                 'nti.transactions.transactions.logger.exception',
                 'nti.transactions.transactions.logger.warning')
    def test_abort_exception_raises(self, fake_begin,
                                    fake_logger, fake_format):
        # begin() returns an object without abort(), which we catch.
        fake_begin.expects_call().returns_fake()

        # Likewise for the things we try to do to log it
        fake_logger.expects_call().raises(ValueError)
        fake_format.expects_call().raises(ValueError)

        def handler():
            raise Exception()
        loop = TransactionLoop(handler)
        assert_that(calling(loop), raises(AbortFailedError))
