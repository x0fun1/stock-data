"""Sealed chronological study of frozen Kronos context and calibration."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import math
import time

import numpy as np
import pandas as pd

from ..common.hashing import canonical_hash, sha256_file
from ..common.jsonio import read_json, write_json
from ..common.timeutil import parse_dt, now_utc
from ..contracts import ResearchRequest, to_dict
from ..models.calibrate import PlattCalibrator
from ..validation.evaluation import evaluate_binary, make_evidence
from ..validation.metrics import date_block_bootstrap
from .orchestration import load_inputs
from .kronos_baseline import daily_history, execute_baseline

VARIANTS = ('raw_all', 'raw_512', 'split_adjusted_512')


def validate_forecast_records(records):
    """Check immutable input/output files even when all blocks are cached."""
    for key, record in records.items():
        forecast_path = Path(record['forecast_file'])
        input_path = Path(record['input_file'])
        if not forecast_path.is_file() or not input_path.is_file():
            raise ValueError('sealed forecast or input file missing')
        if sha256_file(forecast_path) != record['forecast_sha256']:
            raise ValueError('sealed forecast file altered')
        forecast = read_json(forecast_path)
        if sha256_file(input_path) != forecast['input_file_sha256']:
            raise ValueError('sealed input file altered')
        for record_key, forecast_key in [('score','vote_score'),('status','status'),
                ('model_version','model_version'),('input_sha256','input_sha256')]:
            if record.get(record_key) != forecast.get(forecast_key):
                raise ValueError('forecast record disagrees with sealed output')
        if key != record['variant']+':'+record['cutoff']:
            raise ValueError('forecast record key disagrees with context/date')
    return len(records)


def prepare_study_history(prices, ledger, calendar, cutoff, variant):
    if variant not in VARIANTS:
        raise ValueError('unknown Kronos context variant')
    history, excluded = daily_history(prices, calendar, cutoff)
    if variant != 'raw_all':
        history = history[-512:]
    if variant == 'split_adjusted_512':
        history = [dict(bar) for bar in history]
        for bar in history:
            q = ledger.cumulative_q(prices.security_id, bar['market_date'], cutoff.date(), known_by=cutoff)
            for key in ('open', 'high', 'low', 'close'):
                bar[key] /= q
            bar['volume'] *= q
            bar['share_unit_conversion'] = q
    return history, excluded


def _accuracy_ci(values, dates, seed):
    return date_block_bootstrap(dates, values, n_resamples=500, block_days=5, seed=seed)


def run_kronos_study(runtime_root, ticker='NVDA', samples=1000, dev_samples=200, cal_samples=500,
                     study_id='kronos-nvda-1000-v4', progress=None,reuse_study_id=None,calibration_mode='off'):
    if calibration_mode not in {'off','platt-diagnostic'}:
        raise ValueError('Kronos calibration is off or platt-diagnostic; it never replaces the primary raw direction')
    root = Path(runtime_root)
    test_samples = samples - dev_samples - cal_samples
    if min(dev_samples, cal_samples, test_samples) < 2:
        raise ValueError('each chronological block requires at least two target dates')
    inputs = load_inputs(ResearchRequest(market='US', tickers=[ticker]), runtime_root=root)
    identity = inputs.securities.resolve('US', ticker, inputs.cutoff)
    prices, calendar = inputs.price_map[identity.security_id], inputs.calendar
    eligible = [day for day in prices.dates if day < inputs.cutoff and calendar.is_session(day)
        and calendar.next_session(day) in prices.by_date
        and prices.open(calendar.next_session(day)) is not None]
    if len(eligible) < samples:
        raise ValueError(f'insufficient distinct prediction dates: {len(eligible)} < {samples}')
    cutoffs = eligible[-samples:]
    blocks = {'dev': cutoffs[:dev_samples], 'cal': cutoffs[dev_samples:dev_samples+cal_samples],
              'test': cutoffs[dev_samples+cal_samples:]}
    folder = root / 'kronos-studies' / study_id
    protocol = {'study_id':study_id, 'ticker':ticker, 'samples':samples,
        'dev_samples':dev_samples, 'cal_samples':cal_samples, 'test_samples':test_samples,
        'cutoffs':list(map(str, cutoffs)), 'target':'next completed session close strictly greater than its own open',
        'variants':list(VARIANTS), 'selection':'development raw Brier with >=90% forecast coverage',
        'calibrator':'off' if calibration_mode=='off' else 'Platt diagnostic only; fit calibration block only',
        'primary_output':'raw_vote_score','calibration_mode':calibration_mode,'seed':20261108,
        'direction_accuracy_requirement':.60, 'test_policy':inputs.policies.defaults['publication'],
        'source_price_version':inputs.snapshot['datasets']['prices']['version'],
        'source_action_version':inputs.snapshot['datasets'].get('actions',{}).get('version'),
        'historical_first_availability_verified':False, 'test_blindness':'historical masked replay, previously observed public history; not prospective blind evidence',
        'retain_all_artifacts':True, 'data_deletion_allowed':False}
    protocol['maturity_delay_hours']=int(inputs.policies.defaults.get('label',{}).get('maturity_delay_hours',2))
    protocol['boundary_policy']='actual target close plus maturity delay strictly before next block prediction cutoff'
    protocol_path = folder / 'protocol.json'
    if protocol_path.exists():
        if read_json(protocol_path) != protocol:
            raise ValueError('existing study protocol is immutable; choose a new study ID')
    else:
        write_json(protocol_path, protocol)
    records_path = folder / 'forecast-records.json'
    records = read_json(records_path) if records_path.exists() else {}
    if not records and reuse_study_id:
        previous=root/'kronos-studies'/reuse_study_id
        old=read_json(previous/'protocol.json')
        if any(old.get(key)!=protocol.get(key) for key in ('ticker','cutoffs','variants','source_price_version','source_action_version')):
            raise ValueError('reused forecasts must share immutable data and context definitions')
        records=read_json(previous/'forecast-records.json')
        write_json(records_path,records)
        write_json(folder/'reuse.json',{'source_study_id':reuse_study_id,'forecast_records':len(records),
            'selection_and_calibration_reused':False,'all_old_artifacts_preserved':True})
    validate_forecast_records(records)
    completed = len(records)

    def predict(day, variant, block):
        nonlocal completed
        key = variant + ':' + str(day)
        if key in records:
            cached = records[key]
            if not Path(cached['forecast_file']).is_file():
                raise ValueError('sealed forecast file missing')
            if sha256_file(cached['forecast_file']) != cached['forecast_sha256']:
                raise ValueError('sealed forecast file altered')
            return cached
        cutoff = calendar.session_close_utc(day)
        history, excluded = prepare_study_history(prices, inputs.ledger, calendar, cutoff, variant)
        target = calendar.next_session(day)
        forecast = execute_baseline(history, runtime_root=folder, config=inputs.policies.defaults['quant_baseline'],
            ticker=ticker, frequency='1d', cutoff=cutoff,
            target_start=calendar.session_open_utc(target), target_end=calendar.session_close_utc(target),
            excluded=excluded, provenance={'variant':variant, 'block':block,
                'source_price_version':protocol['source_price_version'],
                'adjustment':'only splits through historical cutoff; no future final adjusted series',
                'first_availability_policy':'unknown vendor archive retained as research limitation'})
        row = {'variant':variant,'block':block,'cutoff':str(day),'target':str(target),
            'status':forecast['status'],'score':forecast.get('vote_score'),'direction':forecast.get('direction'),
            'n_history':forecast.get('n_history'),'raw_ohlc_violations':forecast.get('ohlc_order_violations'),
            'paths':forecast.get('path_count'),'forecast_file':forecast['forecast_file'],
            'forecast_sha256':sha256_file(forecast['forecast_file']),
            'input_file':forecast['input_file'],'input_sha256':forecast.get('input_sha256'),
            'model_version':forecast.get('model_version'), 'generated_at':now_utc().isoformat(),
            'contains_actual_target':False}
        records[key] = row
        write_json(records_path, records)
        completed += 1
        if progress and completed % 25 == 0:
            progress({'completed_forecasts':completed,'block':block,'variant':variant})
        return row

    def truth(row):
        target = pd.Timestamp(row['target']).date()
        return int(prices.close(target) > prices.open(target))

    def mature_before(row,next_block):
        target=pd.Timestamp(row['target']).date()
        maturity=calendar.session_close_utc(target)+timedelta(hours=protocol['maturity_delay_hours'])
        return maturity < calendar.session_close_utc(blocks[next_block][0])

    selection_path = folder / 'selection.json'
    if selection_path.exists():
        selection = read_json(selection_path)
    else:
        candidates = []
        for variant in VARIANTS:
            rows = [predict(day, variant, 'dev') for day in blocks['dev']]
            available = [row for row in rows if row['status']=='ok' and row['score'] is not None and mature_before(row,'cal')]
            coverage = len(available) / dev_samples
            loss = float(np.mean([(row['score']-truth(row))**2 for row in available])) if available else None
            candidates.append({'variant':variant,'coverage':coverage,'raw_brier_dev':loss,'evaluated_samples':len(available),
                'boundary_purged':sum(not mature_before(row,'cal') for row in rows)})
        usable = [candidate for candidate in candidates if candidate['coverage'] >= .9]
        if not usable:
            raise ValueError('no development context has sufficient forecast coverage')
        selected = min(usable, key=lambda item:(item['raw_brier_dev'],VARIANTS.index(item['variant'])))['variant']
        selection = {'selected_variant':selected,'development_candidates':candidates,'selection_uses_test':False}
        write_json(selection_path, selection)
    selected = selection['selected_variant']
    calibration_path = folder / 'calibrator.json'
    if calibration_path.exists():
        calibrated = read_json(calibration_path)
        calibrator = PlattCalibrator.from_dict(calibrated['parameters'])
    else:
        cal_rows = [predict(day, selected, 'cal') for day in blocks['cal']]
        usable = [row for row in cal_rows if row['status']=='ok' and row['score'] is not None and mature_before(row,'test')]
        if len(usable) < .9*cal_samples:
            raise ValueError('insufficient calibration forecast coverage')
        calibrator = PlattCalibrator()
        if calibration_mode=='platt-diagnostic':
            calibrator.fit(np.array([row['score'] for row in usable]),np.array([truth(row) for row in usable]))
        calibrated = {'variant':selected,'mode':calibration_mode,'parameters':calibrator.to_dict(),'calibration_rows':len(usable),
            'fit_performed':calibration_mode=='platt-diagnostic','fitted_rows':len(usable) if calibration_mode=='platt-diagnostic' else 0,
            'base_rate':float(np.mean([truth(row) for row in usable])),'calibration_end':str(blocks['cal'][-1]),
            'fit_uses_test':False,'boundary_purged':sum(not mature_before(row,'test') for row in cal_rows)}
        write_json(calibration_path,calibrated)
    test_path = folder / 'test-predictions.json'
    if test_path.exists():
        test_rows = read_json(test_path)
    else:
        test_rows = []
        for day in blocks['test']:
            chosen = predict(day,selected,'test')
            frozen = predict(day,'raw_all','test')
            score = chosen['score'] if chosen['status']=='ok' else None
            calibrated_score = (float(calibrator.predict(np.array([score]))[0]) if calibration_mode=='platt-diagnostic' else score) if score is not None else None
            test_rows.append({'cutoff':str(day),'target':chosen['target'],'raw_selected_score':score,
                'calibrated_score':calibrated_score,'raw_all_score':frozen['score'] if frozen['status']=='ok' else None,
                'selected_forecast':chosen['forecast_file'],'baseline_forecast':frozen['forecast_file']})
        write_json(test_path,test_rows)
        write_json(folder/'test-seal.json',{'sealed_at':now_utc().isoformat(),'prediction_sha256':sha256_file(test_path),
            'selection_sha256':sha256_file(selection_path),'calibrator_sha256':sha256_file(calibration_path),
            'forecast_records_sha256':sha256_file(records_path),
            'contains_test_outcomes':False,'rows':len(test_rows)})
    seal = read_json(folder/'test-seal.json')
    if seal['prediction_sha256'] != sha256_file(test_path):
        raise ValueError('test predictions changed after sealing')
    if seal['selection_sha256']!=sha256_file(selection_path) or seal['calibrator_sha256']!=sha256_file(calibration_path):
        raise ValueError('selection or calibrator changed after test sealing')
    if seal.get('forecast_records_sha256') and seal['forecast_records_sha256']!=sha256_file(records_path):
        raise ValueError('forecast records changed after test sealing')
    if len(test_rows)!=test_samples or [row['cutoff'] for row in test_rows]!=list(map(str,blocks['test'])):
        raise ValueError('sealed test dates disagree with protocol')
    # Outcomes are revealed only after the complete prediction file is sealed.
    frame = pd.DataFrame(test_rows)
    frame['label'] = [int(prices.close(pd.Timestamp(row['target']).date()) > prices.open(pd.Timestamp(row['target']).date())) for row in test_rows]
    frame['base_p'] = calibrated['base_rate']
    frame['cutoff_date'] = frame['target']
    diagnostic_evaluation = evaluate_binary(frame,p_col='calibrated_score',seed=protocol['seed'])
    raw_evaluation = evaluate_binary(frame,p_col='raw_selected_score',seed=protocol['seed'])
    evaluation=raw_evaluation
    evidence = make_evidence(study_id,'US',1,'absolute',evaluation,protocol['test_policy'])
    available = frame['raw_selected_score'].notna()
    hits = ((frame['raw_selected_score']>=.5).astype(int)==frame['label']) & available
    prior_direction = [int(prices.close(pd.Timestamp(row['cutoff']).date()) > prices.open(pd.Timestamp(row['cutoff']).date())) for row in test_rows]
    previous_hits = np.array(prior_direction)==frame['label'].to_numpy()
    difference = hits.to_numpy(float)-previous_hits.astype(float)
    paired_ci = _accuracy_ci(difference,frame['target'],protocol['seed'])
    accuracy_ci = _accuracy_ci(hits.to_numpy(float),frame['target'],protocol['seed'])
    model_version = records[selected+':'+str(blocks['test'][-1])]['model_version']
    published = json.loads((inputs.policies.skill_root/'model-assets/kronos-small/manifest.json').read_text())['checkpoints'][0]['published_at']
    after_publication = pd.Timestamp(test_rows[0]['target']).date() > parse_dt(published).date()
    accuracy = float(hits.mean())
    advantage_verified = (evidence.status.value=='verified' and accuracy_ci['low']>=.60
        and paired_ci['low']>0 and float(available.mean())>=.95 and after_publication)
    result = {'status':'complete','study_id':study_id,'ticker':ticker,'frequency':'1d','model_version':model_version,
        'sample_count':samples,'development_samples':dev_samples,'calibration_samples':cal_samples,
        'test_samples':test_samples,'test_start':test_rows[0]['target'],'test_end':test_rows[-1]['target'],
        'selected_variant':selected,'selection':selection,'calibrator':calibrated,
        'primary_output':'raw_vote_score','calibration_mode':calibration_mode,
        'test_metrics':evaluation['metrics'],'test_evidence':to_dict(evidence),'diagnostic_test_metrics':diagnostic_evaluation['metrics'],
        'direction_comparison':{'raw':raw_evaluation['metrics']['direction'],'calibrated':diagnostic_evaluation['metrics']['direction']},
        'calibrator_diagnostics':calibrator.diagnostics(frame['raw_selected_score'].to_numpy(float)),
        'direction_accuracy':accuracy,'direction_accuracy_ci':accuracy_ci,
        'previous_direction_accuracy':float(previous_hits.mean()),'paired_advantage_ci':paired_ci,
        'always_up_accuracy':float(frame['label'].mean()),'forecast_coverage':float(available.mean()),
        'test_after_checkpoint_publication':after_publication,'statistical_advantage_verified':bool(advantage_verified),
        'probability_verified':False,'historical_first_availability_verified':False,
        'research_scope':'frozen-source historical calibration; no prospective blind-test claim',
        'test_seal':seal,'artifact_directory':str(folder),'retain_all_artifacts':True,'cleanup_performed':False}
    write_json(folder/'test-outcomes.json',frame.to_dict('records'))
    write_json(folder/'result.json',result)
    write_json(root/'validation/kronos-calibration-diagnostic.json',{
        **result,'path_count':16,'method':calibration_mode,'evidence_status':evidence.status.value,
        'reason':evidence.notes,'context_matches_default':selected=='raw_all',
        'scope':'large historical study; context candidate remains separate from the default frozen baseline'})
    return result
