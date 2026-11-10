#!/usr/bin/env python3
"""Credential-free, single-host fenced journal. stdin JSON; stdout JSON. No network."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

class JournalError(Exception):
    pass

def require(value, code='invalid_request'):
    if not value:
        raise JournalError(code)

def identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_.:/@+%\-]{1,512}', value))

STATES = set('dispatching dispatch_pending dispatch_uncertain accepted watching waiting retrying_read watch_stopped watch_incomplete watch_blocked blocked invalid_target read_error target_mismatch no_run run_changed invalid_evidence await_native_wake unexpected_wake run_ended_unsuccessfully needs_settled_wait result_run_changed newer_work_active newer_result needs_result_read already_handled ready_for_review review_pending'.split())
IDS = set('threadId runId messageId taskId projectId resultRunId'.split())
FIELDS = IDS | set('state link dispatchAttempted startPosition lastReadPosition waits attempt errorCode startedAt observedAt handledResultRunIds verifiedResultRunIds continuationEvidenceRef taskComplete'.split())
COMMON = {'action','operationId','owner','generation'}
KEYS = {
 'claim': {'action','operationId','owner','method','args','context','ttlSeconds'},
 'checkpoint': COMMON | {'fields'}, 'read': {'action','operationId'},
 'list': {'action'}, 'release': COMMON,
 'delivery': COMMON | {'state','receiptId'},
}
ARGS = {
 'launch': set('attachments clientRequestId delegatedFrom environmentId interactionMode message modelSelection projectId runtimeMode scratch title workspaceStrategy'.split()),
 'send': set('clientRequestId environmentId message mode threadId'.split()),
}

class Journal:
    def __init__(self, directory, now=time.time):
        self.directory = Path(directory)
        self.now = now

    @contextlib.contextmanager
    def locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077, 'unsafe_directory')
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(self.directory / 'journal.lock', flags, 0o600)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_uid == os.getuid(), 'unsafe_lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self.directory / 'journal.json'
            if path.exists() or path.is_symlink():
                rfd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(rfd) as source:
                    meta = os.fstat(source.fileno())
                    require(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.getuid() and not meta.st_mode & 0o077, 'unsafe_store')
                    data = json.load(source)
                require(isinstance(data, dict) and data.get('version') == 1 and isinstance(data.get('records'), dict), 'invalid_store')
            else:
                data = {'version':1,'records':{}}
            yield data
        finally:
            os.close(fd)

    def save(self, data):
        fd, name = tempfile.mkstemp(prefix='.journal-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(data, output, sort_keys=True, separators=(',',':'))
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.directory / 'journal.json')
            dfd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def snapshot(self):
        # Atomic replacement guarantees a complete old-or-new snapshot without a lock.
        try:
            info = self.directory.lstat()
        except FileNotFoundError:
            return {'version':1,'records':{}}
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077, 'unsafe_directory')
        try:
            fd = os.open(self.directory / 'journal.json', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return {'version':1,'records':{}}
        with os.fdopen(fd) as source:
            meta = os.fstat(source.fileno())
            require(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.getuid() and not meta.st_mode & 0o077, 'unsafe_store')
            data = json.load(source)
        require(isinstance(data, dict) and data.get('version') == 1 and isinstance(data.get('records'), dict), 'invalid_store')
        return data

    def execute(self, request):
        require(isinstance(request, dict))
        action = request.get('action')
        require(action in KEYS and not set(request) - KEYS[action])
        if action != 'list':
            require(identifier(request.get('operationId')))
        if action in ('read','list'):
            records = self.snapshot()['records']
            if action == 'list':
                return {'records':list(records.values())}
            require(request['operationId'] in records, 'not_found')
            return {'record':records[request['operationId']]}
        with self.locked() as data:
            records = data['records']
            op = request.get('operationId')
            record = records.get(op)
            now = self.now()
            require(identifier(request.get('owner')))
            if action == 'claim':
                method = request.get('method')
                require(method in ('launch','send','resume'))
                ttl = request.get('ttlSeconds', 300)
                require(type(ttl) is int and 180 <= ttl <= 86400)
                if method == 'resume':
                    require(record is not None, 'not_found')
                    require('args' not in request and 'context' not in request)
                else:
                    args = request.get('args')
                    require(isinstance(args, dict) and not set(args) - ARGS[method])
                    require(identifier(args.get('environmentId')) and identifier(args.get('clientRequestId')))
                    if method == 'send':
                        require(identifier(args.get('threadId')))
                    context = request.get('context', {})
                    require(isinstance(context, dict) and not set(context) - {'parentTask','acceptanceCriteria','startPosition'})
                    if 'parentTask' in context:
                        require(identifier(context['parentTask']) or type(context['parentTask']) is int and context['parentTask'] >= 0)
                    if 'startPosition' in context:
                        require(type(context['startPosition']) is int and context['startPosition'] >= -1)
                    # Store only digest of context: acceptance criteria may include private text.
                    digest = hashlib.sha256(json.dumps({'method':method,'args':args}, sort_keys=True, separators=(',',':'), allow_nan=False).encode()).hexdigest()
                    for other_op, other in records.items():
                        if other_op != op and other.get('environmentId') == args['environmentId'] and other.get('method') == method and other.get('clientRequestId') == args['clientRequestId']:
                            raise JournalError('request_key_in_use')
                    if record:
                        require(record['requestHash'] == digest, 'request_mismatch')
                    else:
                        record = {'operationId':op,'method':method,'requestHash':digest,'contextHash':hashlib.sha256(json.dumps(context,sort_keys=True,allow_nan=False).encode()).hexdigest(),'environmentId':args['environmentId'],'clientRequestId':args['clientRequestId'],'state':'dispatch_pending','dispatchAttempted':False,'generation':0,'leaseUntil':0,'taskComplete':False}
                        if 'parentTask' in context:
                            record['parentTask'] = context['parentTask']
                        if 'startPosition' in context:
                            record['startPosition'] = context['startPosition']
                        if method == 'send':
                            record['threadId'] = args['threadId']
                require(record.get('leaseUntil',0) <= now, 'owner_busy')
                self.thread_available(records, record, op, now)
                record.update(owner=request['owner'],generation=record['generation']+1,leaseUntil=now+ttl,ttlSeconds=ttl,updatedAt=now)
                records[op] = record
                self.save(data)
                return {'acquired':True,'record':record,'generation':record['generation'],'owner':record['owner'],'operationId':op}
            require(record is not None, 'not_found')
            require(type(request.get('generation')) is int and request['generation'] == record['generation'] and request['owner'] == record.get('owner') and record['leaseUntil'] > now, 'stale_owner')
            if action == 'checkpoint':
                fields = request.get('fields')
                require(isinstance(fields,dict) and not set(fields) - FIELDS)
                self.validate_fields(fields)
                if fields.get('verifiedResultRunIds'):
                    require(identifier(fields.get('continuationEvidenceRef')), 'continuation_proof_required')
                require(record['state'] not in ('delivery_pending','delivery_uncertain','delivered'), 'delivery_locked')
                for key in IDS - {'resultRunId'}:
                    if key in fields and record.get(key) is not None:
                        require(fields[key] == record[key], 'identity_changed')
                if 'dispatchAttempted' in fields:
                    require(fields['dispatchAttempted'] is True and record['dispatchAttempted'] is False, 'dispatch_already_attempted')
                candidate = {**record, **fields}
                self.thread_available(records, candidate, op, now)
                record.update(fields)
            elif action == 'delivery':
                state = request.get('state')
                allowed = {'review_pending':{'delivery_pending'},'ready_for_review':{'delivery_pending'},'delivery_pending':{'delivered','delivery_uncertain'}}
                require(state in allowed.get(record['state'], set()), 'invalid_delivery_transition')
                if 'receiptId' in request:
                    require(state == 'delivered' and identifier(request['receiptId']))
                    record['receiptId'] = request['receiptId']
                record['state'] = state
            elif action == 'release':
                record['leaseUntil'] = 0
            if action != 'release':
                record['leaseUntil'] = now + record['ttlSeconds']
            record['updatedAt'] = now
            self.save(data)
            return {'record':record}

    @staticmethod
    def thread_available(records, record, operation, now):
        if not record.get('threadId'):
            return
        for key, other in records.items():
            if key != operation and other.get('environmentId') == record['environmentId'] and other.get('threadId') == record['threadId'] and other.get('leaseUntil',0) > now:
                raise JournalError('thread_busy')

    @staticmethod
    def validate_fields(fields):
        for key, value in fields.items():
            if key == 'state':
                require(value in STATES)
            elif key in IDS:
                require(value is None or identifier(value))
            elif key in ('startPosition','lastReadPosition'):
                require(value is None or type(value) is int and value >= -1)
            elif key in ('waits','attempt'):
                require(type(value) is int and value >= 0)
            elif key in ('handledResultRunIds','verifiedResultRunIds'):
                require(isinstance(value,list) and len(value) <= 10000 and all(identifier(v) for v in value))
            elif key == 'taskComplete':
                require(value is False)
            elif key == 'dispatchAttempted':
                require(type(value) is bool)
            elif key in ('errorCode','continuationEvidenceRef'):
                require(identifier(value))
            elif key in ('startedAt','observedAt'):
                require(isinstance(value,str) and bool(re.fullmatch(r'[0-9TZ:.+\-]{10,40}',value)))
            elif key == 'link':
                # Arbitrary URLs may contain credential-bearing queries; use external task record instead.
                require(value is None, 'link_not_persisted')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(2_000_001)
        require(len(raw) <= 2_000_000, 'request_too_large')
        result = Journal(args.directory).execute(json.loads(raw))
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    except JournalError as error:
        print(json.dumps({'ok':False,'error':{'code':str(error)}}))
        return 1
    except Exception:
        print(json.dumps({'ok':False,'error':{'code':'journal_error'}}))
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
