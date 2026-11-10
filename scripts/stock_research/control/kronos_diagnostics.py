"""Audit sealed Kronos votes and calibration without refitting or revealing new tests."""
from pathlib import Path

import numpy as np
import pandas as pd

from ..common.hashing import sha256_file
from ..common.jsonio import read_json, write_json
from ..config import load_policies
from ..models.calibrate import PlattCalibrator
from ..storage.curated_store import CuratedStore
from ..validation.evaluation import evaluate_binary, classify_evidence
from .kronos_study import validate_forecast_records


def audit_kronos_study(runtime_root, study_id):
    root=Path(runtime_root)
    folder=root/'kronos-studies'/study_id
    protocol=read_json(folder/'protocol.json')
    seal=read_json(folder/'test-seal.json')
    for name,key in [('test-predictions.json','prediction_sha256'),('selection.json','selection_sha256'),('calibrator.json','calibrator_sha256')]:
        if sha256_file(folder/name)!=seal[key]:raise ValueError('sealed study artifact changed')
    records=read_json(folder/'forecast-records.json')
    verified=validate_forecast_records(records)
    store=CuratedStore(root)
    metadata=store.metadata('prices',protocol['source_price_version'])
    if sha256_file(metadata['path'])[:12]!=protocol['source_price_version'].split('-')[-1]:
        raise ValueError('frozen source prices changed')
    identities=store.load('securities')
    matches=identities[identities['ticker'].str.upper()==protocol['ticker'].upper()]
    if len(matches)!=1:raise ValueError('unique source security identity required')
    prices=store.load('prices',protocol['source_price_version'])
    prices=prices[prices['security_id']==matches.iloc[0]['security_id']].copy()
    prices['date']=pd.to_datetime(prices['date']).dt.date.astype(str)
    quotes=prices.set_index('date')
    rows=read_json(folder/'test-predictions.json')
    if len(rows)!=protocol['test_samples']:raise ValueError('sealed test coverage changed')
    frame=pd.DataFrame(rows)
    frame['label']=[int(quotes.loc[row['target'],'close']>quotes.loc[row['target'],'open']) for row in rows]
    frame['cutoff_date']=frame['target']
    calibration=read_json(folder/'calibrator.json')
    calibrator=PlattCalibrator.from_dict(calibration['parameters'])
    expected=calibrator.predict(frame['raw_selected_score'].to_numpy(float))
    if not np.allclose(expected,frame['calibrated_score'].to_numpy(float),equal_nan=True):
        raise ValueError('sealed calibrated scores disagree with frozen calibrator')
    frame['base_p']=calibration['base_rate']
    frame['always_up']=1.
    frame['always_not_up']=0.
    frame['previous_direction']=[float(quotes.loc[row['cutoff'],'close']>quotes.loc[row['cutoff'],'open']) for row in rows]
    comparisons={name:evaluate_binary(frame,p_col=column,seed=protocol['seed']) for name,column in
        [('raw_votes','raw_selected_score'),('calibrated_votes','calibrated_score'),
         ('always_up','always_up'),('always_not_up','always_not_up'),('previous_direction','previous_direction')]}
    policy=load_policies().defaults['publication']
    state,reason=classify_evidence(comparisons['calibrated_votes'],policy)
    output={'study_id':study_id,'ticker':protocol['ticker'],'status':'audit_complete',
        'scope':'post-hoc diagnostics of an already observed historical holdout; no refit or new blind evidence',
        'source_price_version':protocol['source_price_version'],'verified_input_forecast_pairs':verified,
        'prediction_sha256':seal['prediction_sha256'],'calibrator_sha256':seal['calibrator_sha256'],
        'calibration_mode':calibration.get('mode','legacy-platt'),'primary_output':protocol.get('primary_output','legacy_calibrated'),
        'calibrator':calibrator.diagnostics(frame['raw_selected_score'].to_numpy(float)),
        'comparisons':comparisons,'direction_evidence_status':state.value,'direction_evidence_reason':reason,
        'vote_mapping':[{'up_votes':u,'paths':16,'raw_score':(u+1)/18,
            'calibrated_score':float(calibrator.predict(np.array([(u+1)/18]))[0])} for u in range(17)],
        'neural_weights_changed':False,'calibrator_refitted':False,'cleanup_performed':False}
    target=folder/'direction-audits'/f"{sha256_file(folder/'test-predictions.json')[:12]}-v1.json"
    if target.exists() and read_json(target)!=output:
        raise ValueError('direction audit exists with different rules; use a new audit version')
    write_json(target,output)
    write_json(root/'validation/kronos-direction-diagnostic.json',output)
    return output


def render_direction_comparison(audit):
    lines=['| 方向检验 | 预测上涨占比 | 上涨识别率 | 非上涨识别率 | 平衡准确率 | 总准确率 | AUC |',
        '|---|---|---|---|---|---|---|']
    labels={'raw_votes':'原始路径票数','calibrated_votes':'校准后','always_up':'始终上涨','always_not_up':'始终不上涨','previous_direction':'前日方向'}
    if audit.get('calibration_mode')=='off':labels['calibrated_votes']='校准关闭（原样）'
    def percent(value):return f'{value:.1%}' if value is not None else '不可计算'
    for name in ['raw_votes','calibrated_votes','always_up','always_not_up','previous_direction']:
        evaluation=audit['comparisons'][name]
        d=evaluation['metrics']['direction']
        area=f"{d['auc']:.3f}" if d['auc'] is not None else '不可计算'
        lines.append('| '+labels[name]+' | '+' | '.join(percent(d[key]) for key in
            ['predicted_up_fraction','up_recall','not_up_recall','balanced_accuracy','accuracy'])+f' | {area} |')
    d=audit['comparisons']['calibrated_votes']['metrics']['direction']
    name='原始方向' if audit.get('calibration_mode')=='off' else '校准后'
    lines+=['',f"{name}混淆矩阵（行=真实非上涨/上涨，列=预测非上涨/上涨）：{d['confusion_matrix']['values']}；状态：{audit['direction_evidence_status']}，{audit['direction_evidence_reason']}。"]
    return '\n'.join(lines)
