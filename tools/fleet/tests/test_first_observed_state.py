"""First real state, independent detail refresh, and exact delayed joins."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import details, model, projection, render, route, route_chain
from fleet.collectors import dispatch
from fleet.refresh import LiveSnapshot


class BasicObservationTest(unittest.TestCase):
    def test_basic_projection_reads_no_routes_artifacts_or_ledgers_but_keeps_resource_now(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                owner = model.DispatchJob(key='lab', slug='owner', harness=harness,
                    attempt_id='att-owner', pid=42, proc_start='100', liveness='idle',
                    owner_route_id='rt-work', owner_route_hash='hash', owner_route_file='/fixture/route.json',
                    state_evidence={'inputs': {'observed_liveness': {'state': 'parked-supervised'}}},
                    summary='previous model turn')
                child = model.ResourceJob(run_id='run', parent_attempt_id='att-owner',
                    route_id='rt-work', route_hash='hash', route_file='/fixture/route.json', node='train',
                    liveness='working', pid=43, starttime='101', command=['python', 'train.py'])
                session = model.Session(harness=harness, pid=40, proc_start='90', session_id='parent')
                with mock.patch.object(projection, '_load_evidence_records', side_effect=AssertionError('routes')), \
                        mock.patch.object(projection, '_spec_marker_index', side_effect=AssertionError('markers')), \
                        mock.patch.object(projection, '_artifact_candidates', side_effect=AssertionError('artifacts')), \
                        mock.patch.object(route_chain, 'session_lines', side_effect=AssertionError('ledger')):
                    route_chain.enrich([session], jobs=[owner], fast_first=True)
                    projection.attach_projections([session], [owner], resources=[child], fast_first=True)
                self.assertEqual(owner.work_projection.node_state, 'unknown')
                self.assertIsNone(owner.work_projection.progress)
                self.assertIsNone(owner.work_projection._route_view)
                self.assertEqual(owner.resource_wait['run_ids'], ['run'])
                self.assertEqual(owner.resource_children, [child])
                with mock.patch.object(render, '_fresh_compute_hosts', return_value=(None, None)):
                    now = render._resource_now_text(owner)
                self.assertIn('train train.py', now)
                self.assertIn('호스트/GPU 미확인', now)
                self.assertNotIn('previous model turn', now)
                # A different declared route does not acquire a parked wait.
                child.route_id = 'foreign'
                projection.attach_projections([], [owner], resources=[child], fast_first=True)
                self.assertIsNone(owner.resource_wait)

    def test_basic_now_reads_exact_summary_but_defers_title(self):
        row = model.DispatchJob(key='code', attempt_id='att-x', harness='codex')
        with mock.patch.object(dispatch, '_owned_attempt_log_path', return_value='/owned/log'), \
                mock.patch('fleet.titles.fresh_title', side_effect=AssertionError('title read')), \
                mock.patch('fleet.titles.fresh_summary_with_ts', return_value=('current work', 12)) as summary:
            dispatch._enrich_attempt_summary(row, fast_first=True)
        summary.assert_called_once_with('dispatch-att-x', harness='codex')
        self.assertEqual((row.summary, row.summary_ts), ('current work', 12))

    def test_unknown_basic_now_is_checking_and_does_not_invent_activity(self):
        owner = model.DispatchJob(key='code', harness='opencode', liveness='working')
        projection.attach_projections([], [owner], fast_first=True)
        self.assertIsNone(owner.work_projection.progress)
        text = render._plain(render._context_detail_row(owner, term_width=168)[0])
        self.assertIn('확인 중', text)
        self.assertNotIn('모델 턴', text)
        self.assertIsNone(owner.exec_tool)


    def test_basic_screen_keeps_dotted_owner_flag_now_and_observed_gpu(self):
        saved = {name: getattr(render, name) for name in (
            '_PROCESS_VIEW', '_SHOW_ALL', '_LAYOUT', '_COMPUTE_HOSTS', '_COMPUTE_HOSTS_SET_AT')}
        for name, value in saved.items():
            self.addCleanup(setattr, render, name, value)
        render._PROCESS_VIEW, render._SHOW_ALL, render._LAYOUT = False, False, 'auto'
        parent = model.Session(harness='codex', pid=40, proc_start='90', session_id='parent',
            session_tag='4d', steward=True, steward_targets=[{'harness': 'codex', 'session_id': 'target'}],
            cwd='/work/first-state', model='gpt-6.1-sol', liveness='working')
        target = model.Session(harness='codex', pid=45, proc_start='105', session_id='target',
                               session_tag='f5', cwd=parent.cwd, liveness='idle')
        owner = model.DispatchJob(key='lab', slug='owner', harness='opencode',
            model='gpt-6.1-sol', depth=1, dispatch_depth=1, worker_type='owner',
            parent_sid='parent', is_child=True, cwd=parent.cwd,
            attempt_id='att-owner', pid=42, proc_start='100', liveness='idle',
            owner_route_id='rt-work', owner_route_hash='hash', owner_route_file='/fixture/route.json',
            state_evidence={'inputs': {'observed_liveness': {'state': 'parked-supervised'}}})
        child = model.ResourceJob(run_id='run', parent_attempt_id='att-owner',
            route_id='rt-work', route_hash='hash', route_file='/fixture/route.json', node='train',
            liveness='working', pid=43, starttime='101', process_group=43,
            command=['python', 'train.py'], elapsed_min=2)
        projection.attach_projections([parent, target], [owner], resources=[child], fast_first=True)
        render.set_compute_hosts({'configured': True, 'collected_at': time.time(), 'hosts': [
            {'host': 'moving4', 'reachable': True, 'self': True, 'gpus': [{'index': 1, 'name': 'RTX',
                'processes': [{'pid': 43, 'proc_start': '101', 'pgid': 43, 'command': 'python train.py',
                               'used_memory_mib': 1024}]}]}]})
        with mock.patch.object(route, 'load', side_effect=AssertionError('render route read')), \
                mock.patch.object(render, '_git_branch', side_effect=AssertionError('render git read')):
            rows = render._build_lines([parent, target], [owner], 'both', False, 0,
                                       term_width=168, resources=[child])
        screen = '\n'.join(render._plain(row) for row in rows)
        for required in ('⚑', '┆', '╭', 'owner', 'train train.py', 'moving4:1', 'GPU moving4:1'):
            self.assertIn(required, screen)

    def test_real_detail_pass_fills_cycle_title_and_verified_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = {'schema_version': 1, 'cwd': tmp, 'artifact_root': tmp,
                'capability': 'autopilot-code', 'effective_intensity': 'standard',
                'nodes': [{'id': 'execute', 'depends_on': []}]}
            record['route_hash'] = route.route_hash(record)
            record['route_id'] = 'rt-' + record['route_hash'].split(':')[1][:16]
            path = root / 'route.json'
            path.write_text(json.dumps(record))
            cycles = root / '.runtime/artifact-producer/v1/cycles'
            cycles.mkdir(parents=True)
            (cycles / 'cyc_fixture.json').write_text(json.dumps(
                {'route_id': record['route_id'], 'title': 'actual cycle title'}))
            owner = model.DispatchJob(key='code', slug='owner', harness='codex', cwd=tmp,
                attempt_id='att-owner', pid=42, proc_start='100', liveness='working',
                qa_source='explicit', worker_type='owner', depth=1, artifact_root=tmp, owner_route_id=record['route_id'],
                owner_route_hash=record['route_hash'], owner_route_file=str(path))
            projection.attach_projections([], [owner], fast_first=True)
            source = LiveSnapshot(jobs=[owner])
            with mock.patch.object(dispatch, '_pending_delivery_counts', return_value=None), \
                    mock.patch.object(dispatch, '_scan_degradations', return_value={}), \
                    mock.patch.object(projection, '_spec_marker_index', return_value={}), \
                    mock.patch.object(projection, '_capability_grounding_index', return_value={}), \
                    mock.patch('fleet.collectors.peer_messages.collect', return_value=None):
                filled = details.enrich(source)
            row = details.merge(source, filled).jobs[0]
            self.assertEqual(row.campaign_label, 'actual cycle title')
            self.assertEqual(row.work_projection.source, 'route-exact')
            self.assertIsNotNone(row.work_projection._route_view)
            self.assertIsNone(owner.campaign_label)



class CampaignLabelsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cycles = self.root / '.runtime/artifact-producer/v1/cycles'
        self.cycles.mkdir(parents=True)
        for cache in (dispatch._CYCLE_RECORD_CACHE, dispatch._CYCLE_LABEL_INVENTORIES, dispatch._CYCLE_ROUTE_PATHS):
            cache.clear()

    def record(self, name, rid, title):
        (self.cycles / name).write_text(json.dumps({'route_id': rid, 'title': title}))

    def test_existing_admission_index_reads_one_exact_record_without_inventory(self):
        self.record('cyc_exact.json', 'rt-exact', 'exact title')
        index = self.root / '.runtime/artifact-admission/v1/index.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'artifact_root_id': 'root', 'routes': {
            'foreign-root': {'rt-exact': {'cycle_id': 'cyc_foreign'}},
            'root': {'rt-exact': {'cycle_id': 'cyc_exact'}}}}))
        job = model.DispatchJob(key='code', route_id='rt-exact', artifact_root=str(self.root))
        with mock.patch.object(dispatch.os, 'listdir', side_effect=AssertionError('cycle inventory')):
            dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'exact title')

    def test_open_cycle_fallback_continues_in_bounded_batches_without_stat_sort(self):
        for index in range(401):
            self.record('%03d.json' % index, 'rt-%d' % index, 'title-%d' % index)
        job = model.DispatchJob(key='code', route_id='rt-400', artifact_root=str(self.root))
        original = dispatch._stat_stamp
        observed = []
        def stamp(path):
            observed.append(path)
            return original(path)
        with mock.patch.object(dispatch, '_stat_stamp', side_effect=stamp):
            dispatch._campaign_labels([job])
        self.assertIsNone(job.campaign_label)
        self.assertEqual(sum(Path(p).parent == self.cycles for p in observed), 200)
        dispatch._campaign_labels([job])
        self.assertIsNone(job.campaign_label)
        dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'title-400')
        (self.cycles / '400.json').write_text(json.dumps({'route_id': 'rt-400', 'title': 'updated'}))
        with mock.patch.object(dispatch.os, 'listdir', side_effect=AssertionError('repeat inventory')):
            dispatch._campaign_labels([job])
        self.assertEqual(job.campaign_label, 'updated')
        (self.cycles / '400.json').unlink()
        job.campaign_label = None
        dispatch._campaign_labels([job])
        self.assertIsNone(job.campaign_label)

    def test_two_roots_same_route_id_keep_their_own_titles(self):
        other = self.root / 'other'
        folder = other / '.runtime/artifact-producer/v1/cycles'
        folder.mkdir(parents=True)
        self.record('one.json', 'rt-same', 'first')
        (folder / 'two.json').write_text(json.dumps({'route_id': 'rt-same', 'title': 'second'}))
        jobs = [model.DispatchJob(key='code', route_id='rt-same', artifact_root=str(p))
                for p in (self.root, other)]
        dispatch._campaign_labels(jobs)
        self.assertEqual([j.campaign_label for j in jobs], ['first', 'second'])


class DelayedJoinTest(unittest.TestCase):
    def source(self):
        row = model.DispatchJob(key='code', slug='owner', harness='opencode',
            attempt_id='att-exact', pid=42, proc_start='100', cwd='/fixture', liveness='working',
            route_id='rt-work', route_hash='hash', owner_route_file='/fixture/route.json')
        row._runtime_session_id = 'native-exact'
        row._detail_activity_key = ('log-v1',)
        row.summary, row.summary_ts = 'fresh basic NOW', 20
        row.work_projection = model.WorkProjection(source='registry-exact', node_state='unknown')
        return LiveSnapshot(jobs=[row])

    def filled(self, source):
        value = copy.deepcopy(source)
        row = value.jobs[0]
        row.title, row.campaign_label = 'title', 'campaign'
        row.summary, row.summary_ts = 'older detail NOW', 10
        row.exec_tool = {'name': 'python'}
        row.work_projection = model.WorkProjection(source='route-exact', stage_label='test', node_state='active')
        return details.DetailSnapshot(details.state_key(source), value)

    def test_details_arrive_across_new_activity_without_overwriting_new_now(self):
        source = self.source()
        detail = self.filled(source)
        source.jobs[0]._detail_activity_key = ('log-v2',)
        row = details.merge(source, detail).jobs[0]
        self.assertEqual((row.title, row.campaign_label), ('title', 'campaign'))
        self.assertEqual(row.work_projection.stage_label, 'test')
        self.assertEqual(row.summary, 'fresh basic NOW')
        self.assertIsNone(row.exec_tool)
        self.assertIsNone(source.jobs[0].title)  # copied rows, no mutation of basic source

    def test_attempt_pid_start_route_or_native_session_changes_reject_old_detail(self):
        for field, value in (('attempt_id', 'att-new'), ('proc_start', '101'),
                             ('route_hash', 'new-hash'), ('_runtime_session_id', 'native-new')):
            with self.subTest(field=field):
                source = self.source()
                detail = self.filled(source)
                setattr(source.jobs[0], field, value)
                row = details.merge(source, detail).jobs[0]
                self.assertIsNone(row.title)
                self.assertIsNone(row.exec_tool)
                self.assertEqual(row.work_projection.node_state, 'unknown')

    def test_changed_live_roster_preserves_new_state_and_resource_wait(self):
        source = self.source()
        detail = self.filled(source)
        source.jobs[0].liveness = 'idle'
        source.jobs[0].resource_wait = {'run_ids': ['current-run']}
        row = details.merge(source, detail).jobs[0]
        self.assertEqual(row.title, 'title')
        self.assertEqual(row.liveness, 'idle')
        self.assertEqual(row.resource_wait, {'run_ids': ['current-run']})
        self.assertEqual(row.work_projection.node_state, 'unknown')
        self.assertEqual(row.summary, 'fresh basic NOW')
        self.assertIsNone(row.exec_tool)

    def test_unrelated_activity_does_not_starve_route_detail(self):
        source = self.source()
        unrelated = model.Session(harness='claude', pid=70, proc_start='200',
                                  session_id='unrelated', cwd='/other', liveness='working')
        source.sessions.append(unrelated)
        detail = self.filled(source)
        unrelated.liveness = 'idle'
        source.sessions.append(model.Session(harness='codex', pid=71, proc_start='201',
                                            session_id='new-unrelated', cwd='/other', liveness='working'))
        row = details.merge(source, detail).jobs[0]
        self.assertEqual(row.work_projection.stage_label, 'test')
        self.assertEqual(row.summary, 'fresh basic NOW')

    def test_failed_native_now_read_keeps_active_work_explicitly_checking(self):
        source = self.source()
        source.jobs[0].summary, source.jobs[0].summary_ts = None, None
        detail = self.filled(source)
        detail.snapshot.jobs[0].summary = None
        detail.snapshot.jobs[0].exec_tool = None
        row = details.merge(source, detail).jobs[0]
        text = render._plain(render._context_detail_row(row, term_width=168)[0])
        self.assertIn('확인 중', text)
        self.assertEqual(row.liveness, 'working')


    def test_related_route_child_change_rejects_old_projection(self):
        source = self.source()
        child = model.DispatchJob(key='code-test', attempt_id='att-child', route_id='rt-work',
                                  route_hash='hash', liveness='working')
        source.jobs.append(child)
        detail = self.filled(source)
        child.liveness = 'done'
        row = details.merge(source, detail).jobs[0]
        self.assertIsNone(row.work_projection.stage_label)
        self.assertEqual(row.title, 'title')



class LiveLoopTest(unittest.TestCase):
    def test_real_basic_rows_refresh_while_details_block_then_detail_arrives(self):
        entered, release, basic_second, detail_drawn = [threading.Event() for _ in range(4)]
        self.addCleanup(release.set)
        self.addCleanup(setattr, render, '_REFRESH_HEALTH', render._REFRESH_HEALTH)
        self.addCleanup(setattr, render, '_BLINK_ON', render._BLINK_ON)
        calls, fast_calls, drawn = [], [], []

        def collector(harness_filter=None, fast_first=False):
            calls.append(len(calls) + 1)
            fast_calls.append(fast_first)
            row = model.Session(harness='codex', pid=42, proc_start='100', session_id='exact',
                                liveness='working', title=None)
            row.summary = 'basic-%d' % len(calls)
            row.work_projection = model.WorkProjection(source='none', node_state='unknown')
            return [row], []
        collector.last_resource_jobs = []
        collector.last_usage_snapshots = {}

        def fill(source):
            entered.set()
            self.assertTrue(release.wait(5))
            value = copy.deepcopy(source)
            value.sessions[0].title = 'filled title'
            return details.DetailSnapshot(details.state_key(source), value)
        collector.detail_refresh = fill

        def draw(screen, sessions, jobs, *args, **kwargs):
            if not kwargs.get('loading') and sessions:
                drawn.append((sessions[0].summary, sessions[0].title))
                if sessions[0].summary != 'basic-1':
                    basic_second.set()
                if sessions[0].title == 'filled title':
                    detail_drawn.set()

        class Screen:
            def timeout(self, value):
                pass
            def getch(self):
                time.sleep(.01)
                return ord('q') if detail_drawn.is_set() else -1
            def getmaxyx(self):
                return 50, 168

        result = []
        with mock.patch.object(render, '_init_colors'), \
                mock.patch.object(render.curses, 'curs_set'), \
                mock.patch.object(render, '_draw', side_effect=draw), \
                mock.patch.object(render, '_malformed', return_value=0), \
                mock.patch.object(render, '_collect_governor', return_value=None), \
                mock.patch.object(render, '_collect_memory', return_value=None), \
                mock.patch.object(render.gitinfo, 'enrich_entities'):
            worker = threading.Thread(target=lambda: result.append(render._loop(Screen(), collector, None, 'both', .1)))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(basic_second.wait(2), 'detail blocked a fresh real state')
                self.assertFalse(detail_drawn.is_set())
                release.set()
                self.assertTrue(detail_drawn.wait(2), 'completed detail never arrived')
            finally:
                release.set()
                detail_drawn.set()
                worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [0])
        self.assertTrue(all(fast_calls))
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(any(title is None for now, title in drawn))


if __name__ == '__main__':
    unittest.main()
