#!/usr/bin/env python
"""Auditable historical replay of a pinned Kronos checkpoint and online candidate.

This is a research experiment, not a registered stock-probability publication.
The predictor receives a prefix only. Forecasts are written before labels are
revealed; the online candidate updates after scoring every date, including losses.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def write_once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    with path.open('x', encoding='utf-8') as handle:
        handle.write(content)


def jsonable_frame(frame):
    return json.loads(frame.to_json(orient='records', date_format='iso'))


def phase(day_number):
    if day_number <= 245:
        return 'development'
    if day_number <= 275:
        return 'calibration_observation'
    return 'fixed_rule_evaluation'


class OnlineBlend:
    """Exponentially weighted Brier-loss mixture, with a frozen update rule."""
    def __init__(self, losses=None, decay=0.99, eta=1.0):
        self.losses = np.asarray(losses if losses is not None else [0.0, 0.0], float)
        self.decay, self.eta = decay, eta

    def weights(self):
        z = -self.eta * self.losses
        z -= z.max()
        w = np.exp(z)
        return w / w.sum()

    def predict(self, probabilities):
        return float(self.weights() @ probabilities)

    def update(self, probabilities, observed):
        self.losses = self.decay * self.losses + (np.asarray(probabilities) - observed) ** 2

    def state(self):
        return {'discounted_losses': self.losses.tolist(), 'weights': self.weights().tolist(),
                'decay': self.decay, 'eta': self.eta}


class KronosForecast:
    def __init__(self, root, paths, seed):
        for folder in ('deps', 'deps-extra', 'vendor/pinned-kronos'):
            sys.path.insert(0, str(root / folder))
        import torch
        from model import Kronos, KronosTokenizer, KronosPredictor
        self.torch = torch
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        self.model = Kronos.from_pretrained(str(root / 'checkpoints/mini')).eval()
        self.tokenizer = KronosTokenizer.from_pretrained(str(root / 'checkpoints/tokenizer')).eval()
        self.predictor = KronosPredictor(self.model, self.tokenizer, device='cpu',
                                         max_context=2048, clip=5)
        self.paths, self.seed = paths, seed
        self.version = torch.__version__

    def forecast(self, history, target_date):
        # No actual target values enter this method.
        seed = int(digest({'seed': self.seed, 'date': str(target_date)})[:8], 16)
        self.torch.manual_seed(seed)
        dates = pd.to_datetime(history['date'])
        future_dates = pd.Series(pd.to_datetime([target_date]))
        values = history[['open', 'high', 'low', 'close', 'volume']].copy()
        # The official predictor derives an amount proxy from OHLC mean * volume.
        frames = self.predictor.predict_batch([values] * self.paths, [dates] * self.paths,
            [future_dates] * self.paths, pred_len=1, T=1.0, top_k=0, top_p=0.9,
            sample_count=1, verbose=False)
        paths = [dict(zip(f.columns, map(float, f.iloc[0].values))) for f in frames]
        valid = [p for p in paths if all(math.isfinite(v) for v in p.values())
                 and p['open'] > 0 and p['close'] > 0]
        if len(valid) < max(2, self.paths // 2):
            raise ValueError('too_few_valid_generated_paths')
        intraday_up = sum(p['close'] > p['open'] for p in valid)
        quote_up = sum(p['close'] > float(history.iloc[-1]['close']) for p in valid)
        return {'seed': seed, 'paths': paths, 'valid_paths': len(valid),
                'invalid_ohlc_paths': sum(p['high'] < max(p['open'], p['close'])
                    or p['low'] > min(p['open'], p['close']) for p in valid),
                'intraday_path_up_fraction': intraday_up / len(valid),
                'quote_path_up_fraction': quote_up / len(valid),
                'smoothed_intraday_score': (intraday_up + 1) / (len(valid) + 2),
                'close_median': float(np.median([p['close'] for p in valid])),
                'probability_status': 'uncalibrated_generated_path_score'}


def metrics(rows, name):
    p = np.asarray([r['scores'][name] for r in rows])
    y = np.asarray([r['label_intraday_up'] for r in rows])
    return {'n': len(rows), 'brier': float(np.mean((p-y)**2)),
            'log_loss': float(-np.mean(y*np.log(np.clip(p,1e-6,1-1e-6))
                         +(1-y)*np.log(np.clip(1-p,1e-6,1-1e-6)))),
            'direction_accuracy': float(np.mean((p >= 0.5) == y))}


def paired_block_ci(rows, name, seed=20261107, repetitions=500, block=5):
    baseline=np.asarray([(r['scores']['history_rate']-r['label_intraday_up'])**2 for r in rows])
    candidate=np.asarray([(r['scores'][name]-r['label_intraday_up'])**2 for r in rows])
    rng=np.random.default_rng(seed)
    estimates=[]
    for _ in range(repetitions):
        indices=[]
        while len(indices)<len(rows):
            start=int(rng.integers(0,len(rows)))
            indices.extend((start+j)%len(rows) for j in range(block))
        ix=indices[:len(rows)]
        estimates.append(float(np.mean(baseline[ix]-candidate[ix])))
    return {'metric':'baseline_brier_minus_candidate_brier',
            'mean':float(np.mean(baseline-candidate)),
            'interval_95':np.quantile(estimates,[0.025,0.975]).tolist(),
            'block_days':block,'resamples':repetitions}


def run(args):
    root=Path(args.root)
    lock=json.loads((root/'checkpoint-lock.json').read_text(encoding='utf-8'))
    source_map={'pinned-model-kronos.py':'model/kronos.py',
                'pinned-model-module.py':'model/module.py',
                'pinned-model-__init__.py':'model/__init__.py','pinned-LICENSE':'LICENSE'}
    source_hashes=[]
    for source in lock['source_files']:
        installed=root/'vendor/pinned-kronos'/source_map[Path(source['path']).name]
        observed_hash=hashlib.sha256(installed.read_bytes()).hexdigest()
        if observed_hash!=source['sha256']:
            raise ValueError('vendor_source_hash_mismatch')
        source_hashes.append({'path':str(installed),'sha256':observed_hash})
    for folder in ('deps','deps-extra'):
        sys.path.insert(0,str(root/folder))
    dependency_versions={name:importlib.metadata.version(name) for name in
                         ('torch','huggingface_hub','safetensors','einops')}
    dependency_versions.update(numpy=np.__version__,pandas=pd.__version__,python=sys.version)
    # Verify the exact local files, independently of the download tool.
    files=[]
    for directory in ('mini','tokenizer'):
        for name in ('config.json','model.safetensors'):
            p=root/'checkpoints'/directory/name
            files.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    expected_weights={'mini':'a7d5f37e2e9fbd9891f7d7d4f72574512dd1f704fee14223e0a8cd0fbf54197c',
        'tokenizer':'b97ec46b3b72160509e289183eaf7bdf5f0dac5bb9b49522f6d46638a99a8717'}
    for d,h in expected_weights.items():
        require(hashlib.sha256((root/'checkpoints'/d/'model.safetensors').read_bytes()).hexdigest()==h,
                'checkpoint_weight_hash_mismatch')
    for item in lock['checkpoints']:
        actual_config=hashlib.sha256((root/'checkpoints'/item['name']/'config.json').read_bytes()).hexdigest()
        if actual_config!=item['config_sha256']:
            raise ValueError('checkpoint_config_hash_mismatch')
    frame=pd.read_csv(args.prices)
    if 'ticker' in frame:
        frame=frame[frame['ticker']==args.ticker]
    frame=frame.sort_values('date').tail(365).reset_index(drop=True)
    if len(frame)!=365 or frame['date'].duplicated().any():
        raise ValueError('experiment_requires_365_unique_trading_bars')
    if frame[['open','high','low','close','volume']].isna().any().any():
        raise ValueError('missing_ohlcv')
    if (frame[['open','high','low','close']]<=0).any().any():
        raise ValueError('nonpositive_quotes')
    from stock_research.identity_calendar.calendar import build_us_calendar
    first=pd.Timestamp(frame.iloc[0]['date']).date()
    last=pd.Timestamp(frame.iloc[-1]['date']).date()
    calendar=build_us_calendar(first.year-1,last.year+1)
    known_extra_closures={pd.Timestamp('2025-01-09').date()}
    expected=[d.isoformat() for d in calendar.sessions(first,last) if d not in known_extra_closures]
    require(list(frame['date'])==expected,'daily_data_gap_or_non_session_in_selected_window')
    published_date=max(pd.Timestamp(c['published_at']).date() for c in lock['checkpoints'])
    checkpoint_preexisted_labels=pd.Timestamp(frame.iloc[180]['date']).date()>published_date
    protocol={'version':'rolling-kronos-v1','ticker':args.ticker,'observations':365,'context':180,
        'data_hash':digest(jsonable_frame(frame)),'input_file':str(Path(args.prices).resolve()),
        'data_start':str(frame.iloc[0]['date']),'data_end':str(frame.iloc[-1]['date']),
        'partitions':{'initial':[1,180],'development':[181,245],
                      'calibration_observation':[246,275],'fixed_rule_evaluation':[276,365]},
        'target':'next_session_open_to_close_quote_direction',
        'auxiliary_target':'next_session_close_quote','no_total_return_probability_claim':True,
        'paths':args.paths,'seed':args.seed,'sampling':{'T':1.0,'top_p':0.9,'top_k':0},
        'model':'NeoQuasar/Kronos-mini','weights':files,'checkpoint_lock_hash':digest(lock),
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'vendor_sources':source_hashes,'dependencies':dependency_versions,
        'calendar':{'version':calendar.coverage_label(),'extra_closures':['2025-01-09'],
                    'source':'https://www.nasdaqtrader.com/TraderNews.aspx?id=ETA2024-87'},
        'checkpoint_preexisted_scored_labels':checkpoint_preexisted_labels,
        'history_policy':'rolling_180_actual_bars','online_rule':{'decay':0.99,'eta':1.0},
        'status':'historical_research_replay_not_active_verified_model',
        'qualification':'No hyperparameter search or calibrated probability publication; evaluation has 90 dates.'}
    run_id=digest(protocol)[:16]
    output=root/'runs'/run_id
    output.mkdir(parents=True,exist_ok=True)
    if (output/'protocol.json').exists():
        require(json.loads((output/'protocol.json').read_text(encoding='utf-8'))==protocol,
                'stored_protocol_mismatch')
    else:
        write_once(output/'protocol.json',protocol)
        write_once(output/'input.json',jsonable_frame(frame))
        (output/'rolling_baseline.snapshot.py').write_bytes(Path(__file__).read_bytes())
    forecaster=KronosForecast(root,args.paths,args.seed)
    blend=OnlineBlend()
    all_rows=[]
    stop=min(365,180+args.max_predictions) if args.max_predictions else 365
    for i in range(180,stop):
        history=frame.iloc[i-180:i].copy().reset_index(drop=True)
        target_date=str(frame.iloc[i]['date']) # schedule only; no target quote yet
        forecast_path=output/'forecasts'/f'{target_date}.json'
        scored_path=output/'scored'/f'{target_date}.json'
        historical_y=(history['close']>history['open']).astype(int)
        base=float((historical_y.sum()+1)/(len(historical_y)+2))
        if forecast_path.exists():
            forecast=json.loads(forecast_path.read_text(encoding='utf-8'))
            require(forecast['input_hash']==digest(jsonable_frame(history)),'stored_forecast_input_mismatch')
            require(forecast['online_state_before']==blend.state(),'stored_forecast_online_state_mismatch')
        else:
            try:
                generated=forecaster.forecast(history,target_date)
            except Exception as exc:
                failure=output/'failures'/f'{target_date}.json'
                if not failure.exists():
                    write_once(failure,{'target_date':target_date,'stage':'forecast_before_label_reveal',
                        'error_type':type(exc).__name__,'input_hash':digest(jsonable_frame(history)),
                        'status':'halted_preserving_previous_forecasts_no_label_revealed'})
                raise
            scores={'history_rate':base,'kronos':generated['smoothed_intraday_score']}
            scores['adaptive_blend']=blend.predict([base,scores['kronos']])
            forecast={'target_date':target_date,'cutoff_date':str(history.iloc[-1]['date']),
                'day_number':i+1,'phase':phase(i+1),'input_hash':digest(jsonable_frame(history)),
                'context_rows':len(history),'last_close':float(history.iloc[-1]['close']),
                'scores':scores,'generated':generated,'online_state_before':blend.state(),
                'status':'forecast_sealed_before_label_reveal',
                'recorded_at':datetime.now(timezone.utc).isoformat()}
            write_once(forecast_path,forecast)
        # The label is revealed only after a forecast has been written successfully.
        actual=frame.iloc[i]
        observed=int(actual['close']>actual['open'])
        blend.update([base,forecast['scores']['kronos']],observed)
        scored={**forecast,'label_intraday_up':observed,'actual_open':float(actual['open']),
            'actual_close':float(actual['close']),'actual_intraday_return':float(actual['close']/actual['open']-1),
            'forecast_sha256':hashlib.sha256(forecast_path.read_bytes()).hexdigest(),
            'online_state_after':blend.state(),'status':'scored_then_candidate_updated'}
        if scored_path.exists():
            require(json.loads(scored_path.read_text(encoding='utf-8'))==scored,'stored_score_mismatch')
        else:
            write_once(scored_path,scored)
        all_rows.append(scored)
        if len(all_rows)%10==0 or len(all_rows)==1:
            print(json.dumps({'run_id':run_id,'scored':len(all_rows),'date':target_date,
                              'status':'all_dates_recorded'},ensure_ascii=False),flush=True)
    groups={p:[r for r in all_rows if r['phase']==p] for p in
            ('development','calibration_observation','fixed_rule_evaluation')}
    result={'run_id':run_id,'output':str(output),'status':protocol['status'],
        'scored_dates':len(all_rows),'evaluation':{},'torch_version':forecaster.version,
        'checkpoint_status':'weights_preexisted_all_scored_labels' if checkpoint_preexisted_labels
                            else 'checkpoint_may_have_seen_scored_labels',
        'probability_publication':'not_eligible_90_evaluation_dates_and_uncalibrated_scores',
        'online_state_final':blend.state()}
    for p,rows in groups.items():
        if not rows:
            continue
        result['evaluation'][p]={name:metrics(rows,name) for name in
                                 ('history_rate','kronos','adaptive_blend')}
        result['evaluation'][p]['always_up_accuracy']=float(np.mean([r['label_intraday_up'] for r in rows]))
        result['evaluation'][p]['close_mae']={
            'previous_close':float(np.mean([abs(r['last_close']-r['actual_close']) for r in rows])),
            'kronos_median':float(np.mean([abs(r['generated']['close_median']-r['actual_close']) for r in rows]))}
        result['evaluation'][p]['paired_brier_difference']={name:paired_block_ci(rows,name)
                                                         for name in ('kronos','adaptive_blend')}
    summary_path=output/'summary.json'
    summary_path.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    if stop==365:
        (root/'latest-run.json').write_text(json.dumps({'run_id':run_id,'summary':str(summary_path)},indent=2),encoding='utf-8')
    pd.DataFrame([{'date':r['target_date'],'phase':r['phase'],'actual_open':r['actual_open'],
        'actual_close':r['actual_close'],'label_intraday_up':r['label_intraday_up'],
        'history_rate':r['scores']['history_rate'],'kronos_score':r['scores']['kronos'],
        'adaptive_score':r['scores']['adaptive_blend'],'kronos_close_median':r['generated']['close_median'],
        'kronos_weight_before':r['online_state_before']['weights'][1]} for r in all_rows]).to_csv(output/'daily-scores.csv',index=False)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default=str(WORKSPACE/'runtime/walk-forward-baseline'))
    parser.add_argument('--prices',required=True)
    parser.add_argument('--ticker',default='NVDA')
    parser.add_argument('--paths',type=int,default=16)
    parser.add_argument('--seed',type=int,default=20261107)
    parser.add_argument('--max-predictions',type=int,default=0,help='pipeline smoke limit, does not alter protocol')
    run(parser.parse_args())
