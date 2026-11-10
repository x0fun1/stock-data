"""Reusable dimensionless features from sealed forecasts; no neural inference."""
from pathlib import Path
from datetime import timedelta

import numpy as np
import pandas as pd

from ..common.hashing import canonical_hash,sha256_file
from ..common.jsonio import read_json,write_json
from ..common.timeutil import now_utc
from ..identity_calendar import build_us_calendar
from ..storage.curated_store import CuratedStore
from .kronos_study import validate_forecast_records

FEATURES=['kronos_score','kronos_return_median','kronos_return_iqr','kronos_return_std','kronos_ohlc_invalid_fraction']


def export_kronos_bank(runtime_root,study_id):
    root=Path(runtime_root);folder=root/'kronos-studies'/study_id
    protocol=read_json(folder/'protocol.json');selection=read_json(folder/'selection.json')
    variant=selection['selected_variant'];all_records=read_json(folder/'forecast-records.json')
    records={key:row for key,row in all_records.items() if row['variant']==variant}
    validate_forecast_records(records)
    store=CuratedStore(root)
    prices=store.load('prices',protocol['source_price_version'])
    metadata=store.metadata('prices',protocol['source_price_version'])
    if sha256_file(metadata['path'])[:12]!=protocol['source_price_version'].split('-')[-1]:raise ValueError('source prices changed')
    identities=store.load('securities');identity=identities[identities['ticker'].str.upper()==protocol['ticker'].upper()]
    if len(identity)!=1:raise ValueError('unique security identity required')
    sid=identity.iloc[0]['security_id'];prices=prices[prices['security_id']==sid].copy()
    prices['date']=pd.to_datetime(prices['date']).dt.date.astype(str);quotes=prices.set_index('date')
    dates=protocol['cutoffs'];dev_end=protocol['dev_samples'];cal_end=dev_end+protocol['cal_samples']
    calendar=build_us_calendar(pd.Timestamp(dates[0]).year,pd.Timestamp(dates[-1]).year+1)
    asof=now_utc();rows=[]
    for index,cutoff in enumerate(dates):
        record=records[variant+':'+cutoff];forecast=read_json(record['forecast_file'])
        returns=[]
        for path in forecast.get('paths',[]):
            opening,closing=path.get('open'),path.get('close')
            if opening and closing and np.isfinite(opening) and np.isfinite(closing) and opening>0 and closing>0:
                returns.append(closing/opening-1)
        path_count=forecast.get('path_count',0)
        target=record['target'];target_day=pd.Timestamp(target).date()
        matured=calendar.session_close_utc(target_day)+timedelta(hours=protocol['maturity_delay_hours'])
        label=int(quotes.loc[target,'close']>quotes.loc[target,'open']) if matured<=asof else None
        partition='dev' if index<dev_end else 'cal' if index<cal_end else 'test'
        next_index=dev_end if partition=='dev' else cal_end if partition=='cal' else None
        purged=bool(next_index is not None and matured>=calendar.session_close_utc(pd.Timestamp(dates[next_index]).date()))
        feature={'kronos_score':record.get('score'),
            'kronos_return_median':float(np.median(returns)) if returns else None,
            'kronos_return_iqr':float(np.quantile(returns,.75)-np.quantile(returns,.25)) if returns else None,
            'kronos_return_std':float(np.std(returns)) if returns else None,
            'kronos_ohlc_invalid_fraction':forecast.get('ohlc_order_violations',0)/path_count if path_count and record['status']=='ok' else None}
        rows.append({'security_id':sid,'ticker':protocol['ticker'],'cutoff_date':cutoff,'target_date':target,
            'partition':partition,'label':label,'matured_at':matured.isoformat(),'boundary_purged':purged,
            'forecast_status':record['status'],'model_version':record.get('model_version'),
            'forecast_file':record['forecast_file'],'forecast_sha256':record['forecast_sha256'],
            'input_file':record['input_file'],'input_sha256':record.get('input_sha256'),
            **{'f__'+key:value for key,value in feature.items()},
            **{'m__'+key:int(value is None) for key,value in feature.items()}})
    frame=pd.DataFrame(rows)
    signature=canonical_hash(rows);output=folder/'feature-banks'/f'kronos-path-features-v2-{signature[:12]}.parquet'
    if not output.exists():
        output.parent.mkdir(parents=True,exist_ok=True);frame.to_parquet(output,index=False)
    result={'study_id':study_id,'variant':variant,'feature_version':'kronos-path-features-v2','rows':len(frame),'features':FEATURES,'path':str(output),
        'data_sha256':sha256_file(output),'rows_signature':signature,'neural_inference_calls':0,
        'neural_weights_changed':False,'uses_hidden_embeddings':False,'feature_definition':'predicted close/open-1 and dispersion; score and geometry quality',
        'feature_columns':['f__'+key for key in FEATURES],'labels_separate_from_feature_columns':True,
        'historical_first_availability_verified':False,'scope':'previously observed study, reusable development features; not new blind-test evidence',
        'cleanup_performed':False}
    write_json(output.with_suffix('.manifest.json'),result)
    return result
