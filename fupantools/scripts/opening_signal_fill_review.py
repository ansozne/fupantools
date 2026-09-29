#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D1_STRONG_CS 开盘信号：收盘后模拟成交回顾。"""
from __future__ import annotations

import argparse, json, sys, glob, os, re
from pathlib import Path
from datetime import datetime, time, timezone, timedelta
from statistics import mean, median

ROOT = Path(os.environ.get('REVIEW_WORKDIR', Path(__file__).resolve().parents[1])).expanduser().resolve()
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'output' / 'opening_signal' / 'fill_review'

from scripts.rolling_backtest_baseline_v1 import query_day_prices, safe_float, INITIAL_CAPITAL, _calc_limit_prices, date_cn, extract_price, field_dates_match_query, is_at_price_limit, is_limit_up, can_sell_today, calc_sell_price
from stock_analyzer.core.ifind_api import query_by_date

STATE = OUT / 'd1_strong_cs_paper_state.json'
TP_PCT = None  # DI主基线口径：不使用固定止盈
SL_PCT = None  # DI主基线口径：不使用固定止损
MAX_HOLD_DAYS = None  # DI主基线口径：不使用固定持有期



def calc_d1_open_buy_price(open_price: float, limit_up_price: float | None = None) -> tuple:
    """D1 fill-review entry price: 挂涨停价，成交价严格等于开盘价。"""
    if limit_up_price and is_at_price_limit(open_price, limit_up_price):
        return None, f'一字涨停(开{open_price:.2f}=涨停{limit_up_price:.2f})'
    return round(float(open_price), 4), None

def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {'cash': None, 'positions': [], 'closed_trades': []}


def save_state(state):
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def rewind_state_for_date(state: dict, date: str) -> dict:
    """Rebuild paper state to the moment just before ``date``.

    The close-review job can run more than once on the same trade date.
    Without rewinding, the first
    run writes today's simulated buys into the state book; the next run then
    sees the same signal names as existing positions and reports them as
    "已有持仓，不重复买入". That is not a real non-fill, just a rerun artifact.

    Rewind all trades dated >= date, then replay today's signal from a clean
    pre-date book so repeated runs are idempotent.
    """
    state = dict(state or {})
    cash = state.get('cash')
    if cash is None:
        return state
    cash = float(cash or 0)

    # Undo buys opened on/after the replay date.
    kept_positions = []
    for pos in list(state.get('positions') or []):
        if str(pos.get('entry_date') or '') >= date:
            entry_value = safe_float(pos.get('entry_value'), None)
            if entry_value is None:
                entry_value = int(pos.get('shares') or 0) * float(pos.get('entry_price') or 0)
            cash += float(entry_value or 0)
        else:
            kept_positions.append(pos)

    # Undo sells closed on/after the replay date and restore the pre-sell positions.
    kept_closed = []
    restore_positions = []
    exit_fields = {'exit_date', 'exit_price', 'exit_type', 'exit_value', 'pnl', 'ret_pct'}
    for tr in list(state.get('closed_trades') or []):
        if str(tr.get('exit_date') or '') >= date:
            cash -= float(tr.get('exit_value') or 0)
            pos = {k: v for k, v in tr.items() if k not in exit_fields}
            pos['last_price'] = pos.get('last_price') or pos.get('entry_price')
            restore_positions.append(pos)
        else:
            kept_closed.append(tr)

    # Avoid duplicate restored lots if a partially rewound state is encountered.
    seen = set()
    positions = []
    for pos in kept_positions + restore_positions:
        key = (pos.get('entry_date'), pos.get('code'), pos.get('shares'), pos.get('entry_price'))
        if key in seen:
            continue
        seen.add(key)
        positions.append(pos)

    state['cash'] = round(cash, 2)
    state['positions'] = positions
    state['closed_trades'] = kept_closed
    state['rewound_for_date'] = date
    return state


def _local_stock_row_is_eod(row: dict, date: str) -> bool:
    """Accept only parseable same-day post-close collection timestamps (Shanghai)."""
    fetched_at = row.get('fetched_at')
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(str(fetched_at).replace('Z', '+00:00'))
        shanghai = timezone(timedelta(hours=8))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=shanghai)  # Local warehouse timestamps use Shanghai wall time.
        ts = ts.astimezone(shanghai)
    except (TypeError, ValueError, OverflowError):
        return False
    if ts.date().isoformat() != date:
        return False
    return ts.time() >= time(15, 0)


def query_local_stock_db_prices(code: str, date: str) -> dict | None:
    path = ROOT / '股票历史数据库' / 'stocks' / date[:4] / f'{date}.parquet'
    if not path.exists():
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(path)
        row = df[df['code'].astype(str) == code]
        if row.empty:
            return None
        r = row.iloc[0].to_dict()
        if not _local_stock_row_is_eod(r, date):
            return None
        prev_close = None
        try:
            from scripts.rolling_backtest_baseline_v1 import _local_prev_trade_date, _load_local_stock_day
            prev_date = _local_prev_trade_date(date)
            prev_row = _load_local_stock_day(prev_date).get(code, {})
            prev_close = safe_float(prev_row.get('close'), None)
        except Exception:
            prev_close = None
        limit_up, limit_down = _calc_limit_prices(prev_close, code, str(r.get('name') or ''), date)
        return {
            'open': safe_float(r.get('open'), None),
            'close': safe_float(r.get('close'), None),
            'high': safe_float(r.get('high'), None),
            'low': safe_float(r.get('low'), None),
            'change': safe_float(r.get('pct_chg'), safe_float(r.get('change'), None)),
            'limit_up_price': limit_up,
            'limit_down_price': limit_down,
            'prev_close': prev_close,
            'ifind_field_date_valid': True,
            'price_source': 'local_stock_db_eod',
            'close_is_proxy': False,
        }
    except Exception as exc:
        print(f'local stock DB price query failed: {code} {date} {exc}', file=sys.stderr)
        return None


def _mx_num(v):
    if v is None:
        return None
    s = str(v).strip().replace(',', '')
    if not s or s in {'-', '--', 'None', 'nan'}:
        return None
    s = re.sub(r'(元|%|％|倍|万|亿)$', '', s)
    try:
        return float(s)
    except Exception:
        m = re.search(r'-?\d+(?:\.\d+)?', s)
        return float(m.group(0)) if m else None


def query_mx_data_prices(code: str, date: str) -> dict | None:
    """Credential-dependent MX fallback intentionally omitted from the export."""
    return None


def query_close_review_prices(code: str, date: str) -> dict:
    """Fetch EOD prices for fill review.

    The opening signal review runs after close. If local stock parquet is absent,
    force an iFinD date query instead of using auction/prev-close proxy as close.
    """
    local_px = query_local_stock_db_prices(code, date)
    if local_px and local_px.get('close') is not None:
        return local_px

    px = query_day_prices(code, date)
    if (
        px
        and px.get('open') is not None
        and px.get('close') is not None
        and px.get('ifind_field_date_valid') is True
        and not px.get('close_is_proxy')
    ):
        px = dict(px)
        px['price_source'] = px.get('source') or px.get('price_source') or 'ifind_eod_query_by_date'
        px['close_is_proxy'] = False
        return px
    try:
        res = query_by_date(date_cn(date), code, ['开盘价', '收盘价', '最高价', '最低价', '涨跌幅', '涨停价', '跌停价', '前收盘价'])
        table = res.get('data') or {}
        valid_date = field_dates_match_query(table, date, keywords=['开盘价', '收盘价', '最高价', '最低价', '涨跌幅', '涨停价', '跌停价', '前收盘价'])
        if valid_date and extract_price(table, '开盘价') is not None and extract_price(table, '收盘价') is not None:
            return {
                'open': extract_price(table, '开盘价'),
                'close': extract_price(table, '收盘价'),
                'high': extract_price(table, '最高价'),
                'low': extract_price(table, '最低价'),
                'change': extract_price(table, '涨跌幅'),
                'limit_up_price': extract_price(table, '涨停价'),
                'limit_down_price': extract_price(table, '跌停价'),
                'prev_close': extract_price(table, '前收盘价'),
                'ifind_field_date_valid': True,
                'price_source': 'ifind_eod_query_by_date',
                'close_is_proxy': False,
            }
    except Exception as exc:
        print(f'iFinD EOD price query failed: {code} {date} {exc}', file=sys.stderr)

    return {}

def enrich_price_from_order(px: dict, order: dict) -> dict:
    """Never synthesize an open/close from auction or position fields."""
    return dict(px or {})


def require_eod_prices(code: str, date: str) -> dict:
    px = query_close_review_prices(code, date)
    if (px.get('ifind_field_date_valid') is not True or px.get('close_is_proxy')
            or any(safe_float(px.get(k), None) is None or safe_float(px[k], None) <= 0 for k in ('open', 'close'))):
        raise ValueError(f'{date} {code}: 缺少同日可靠开盘/收盘价；不写模拟账本或盈亏')
    return px


def calculate_hold_days(entry_date: str, current_date: str) -> int:
    """Idempotent holding-day count for review reruns.

    Same trade date is day 1. Re-running the same date must not increment hold_days.
    """
    if not entry_date or not current_date:
        return 1
    if entry_date >= current_date:
        return 1
    try:
        from scripts.rolling_backtest_baseline_v1 import is_a_share_trading_day
        from datetime import datetime, timedelta
        start = datetime.strptime(entry_date, '%Y-%m-%d').date()
        end = datetime.strptime(current_date, '%Y-%m-%d').date()
        days = 0
        d = start
        while d <= end:
            ds = d.strftime('%Y-%m-%d')
            if is_a_share_trading_day(ds) or ds in (entry_date, current_date):
                days += 1
            d += timedelta(days=1)
        return max(days, 1)
    except Exception:
        return 1

def execute_paper_review(date: str, sig: dict) -> dict:
    capital=float(sig.get('capital') or INITIAL_CAPITAL or 1_000_000)
    state=rewind_state_for_date(load_state(), date)
    if state.get('cash') is None:
        state['cash']=capital
    cash=float(state.get('cash') or 0)
    positions=list(state.get('positions') or [])
    closed=list(state.get('closed_trades') or [])
    if any(p.get('last_close_is_proxy') for p in positions):
        raise ValueError('历史持仓含收盘代理价；须先核对/清理账本，不继续计算盈亏')
    # Resolve all prices before any state changes; one missing close aborts the entire review.
    prices = {code: require_eod_prices(code, date) for code in
              {p['code'] for p in positions} | {o['code'] for o in sig.get('buy_orders', [])}}
    today_rows=[]
    rows=[]; sells=[]; buys=[]

    # 1) exits first: DI主基线最小复刻：T+1 后非涨停直接卖；涨停/跌停均持有。
    remaining=[]
    for pos in positions:
        px=prices[pos['code']]
        pos=dict(pos); pos['trade_date']=date
        px=enrich_price_from_order(px, pos)
        day_open=safe_float(px.get('open'), None)
        day_high=safe_float(px.get('high'), None)
        day_low=safe_float(px.get('low'), None)
        day_close=safe_float(px.get('close'), None)
        # Compute this before the missing-close branch as well; otherwise a
        # stale/missing price leaves hold_days unbound and aborts the review.
        hold_days=calculate_hold_days(pos.get('entry_date'), date)
        if day_close is None or day_close <= 0:
            pos['hold_days']=hold_days
            pos['hold_reason']='行情未落库/收盘价缺失，保留持仓不模拟卖出'
            pos['last_price_source']=px.get('price_source', px.get('source'))
            pos['last_close_is_proxy']=px.get('close_is_proxy', False)
            remaining.append(pos)
            continue
        # query_day_prices currently has no high in old wrapper; if missing, use close/open as weak fallback.
        if day_high is None:
            vals=[v for v in [day_open, day_close] if v is not None]
            day_high=max(vals) if vals else None
        entry=float(pos['entry_price'])
        shares=int(pos['shares'])
        # 回放幂等保护：同日入场仓位不参与当日卖出，避免重复回放污染账本后被当成 T+1。
        if pos.get('entry_date') >= date:
            pos['hold_days']=hold_days
            if day_close is not None and not px.get('close_is_proxy'):
                pos['last_price']=day_close
                pos['last_price_source']=px.get('price_source', px.get('source'))
                pos['last_close_is_proxy']=False
            remaining.append(pos)
            continue
        exit_type=None; raw_exit=None
        hold_by_limit_up = is_limit_up(pos['code'], pos.today_change_close if hasattr(pos, 'today_change_close') else safe_float(px.get('change'), None), pos.get('name', ''), close_price=day_close, limit_up_price=px.get('limit_up_price'))
        cannot_sell_by_limit_down = not can_sell_today(pos['code'], safe_float(px.get('change'), None), pos.get('name', ''), close_price=day_close, limit_down_price=px.get('limit_down_price'))
        # 当日新仓不在 exit 阶段；历史持仓按 DI：非涨停且非跌停可卖则直接卖出。
        if hold_by_limit_up:
            pos['hold_reason']='limit_up_hold'
        elif cannot_sell_by_limit_down:
            pos['hold_reason']='limit_down_or_cannot_sell_hold'
        elif day_close is not None:
            exit_type='not_limit_up_next_day'; raw_exit=day_close
        if exit_type and raw_exit:
            exit_price=calc_sell_price(raw_exit)
            value=shares*exit_price
            pnl=(exit_price-entry)*shares
            cash += value
            tr={**pos,'exit_date':date,'exit_price':exit_price,'exit_type':exit_type,'exit_value':round(value,2),'pnl':round(pnl,2),'ret_pct':round((exit_price/entry-1)*100,4),'hold_days':hold_days}
            closed.append(tr); sells.append(tr)
        else:
            pos['hold_days']=hold_days
            if day_close is not None and not px.get('close_is_proxy'):
                pos['last_price']=day_close
                pos['last_price_source']=px.get('price_source', px.get('source'))
                pos['last_close_is_proxy']=False
            remaining.append(pos)
    positions=remaining
    starting_positions=positions
    new_positions=[]

    # 2) buys at open according to D1 signal weights.
    total_plan_weight=0.0; total_plan_amount=0.0; total_fill_weight=0.0; total_fill_amount=0.0
    for o in sig.get('buy_orders', []):
        code=o.get('code'); name=o.get('name','')
        weight=float(o.get('weight') or 0)
        plan_amount=round(capital*weight,2)
        total_plan_weight += weight; total_plan_amount += plan_amount
        if any(p.get('code')==code for p in starting_positions):
            row={**o,'action':'BUY','filled':False,'fill_reason':'已有持仓，不重复买入','plan_amount':plan_amount}
            rows.append(row); continue
        px=prices[code]
        o=dict(o); o['trade_date']=date
        px=enrich_price_from_order(px, o)
        day_open=safe_float(px.get('open'), None)
        day_close=safe_float(px.get('close'), None)
        prev_close=safe_float(px.get('prev_close'), None)
        lu=safe_float(px.get('limit_up_price'), None)
        if day_open is None or day_open <= 0:
            row={**o,'action':'BUY','filled':False,'fill_reason':'行情未落库/开盘价缺失，保留计划不模拟成交','open':day_open,'close':day_close,'prev_close':prev_close,'limit_up_price':lu,'plan_amount':plan_amount}
            rows.append(row); continue
        buy_px, reason=calc_d1_open_buy_price(day_open, limit_up_price=lu)
        if buy_px is None:
            row={**o,'action':'BUY','filled':False,'fill_reason':reason,'open':day_open,'close':day_close,'prev_close':prev_close,'limit_up_price':lu,'plan_amount':plan_amount}
            rows.append(row); continue
        affordable=min(plan_amount, cash)
        shares=int(affordable/buy_px/100)*100
        fill_amount=round(shares*buy_px,2)
        if shares < 100:
            row={**o,'action':'BUY','filled':False,'fill_reason':'现金/目标金额不足100股','open':day_open,'close':day_close,'buy_price':buy_px,'plan_amount':plan_amount}
            rows.append(row); continue
        cash -= fill_amount
        fill_weight=fill_amount/capital if capital else 0
        total_fill_weight += fill_weight; total_fill_amount += fill_amount
        pos={'entry_date':date,'code':code,'name':name,'rank':o.get('rank'),'theme':o.get('theme'),'weight':weight,'entry_price':buy_px,'shares':shares,'entry_value':fill_amount,'last_price':day_close or buy_px,'hold_days':calculate_hold_days(date, date),'source_signal':sig.get('archive_path') or sig.get('signal_path'),'last_price_source':px.get('price_source', px.get('source')),'last_close_is_proxy':px.get('close_is_proxy', False)}
        new_positions.append(pos); buys.append(pos)
        rows.append({**o,'action':'BUY','filled':True,'fill_reason':'','open':day_open,'close':day_close,'prev_close':prev_close,'limit_up_price':lu,'buy_price':buy_px,'shares':shares,'plan_amount':plan_amount,'fill_amount':fill_amount,'fill_weight':round(fill_weight,6),'day_ret_pct':round((day_close/buy_px-1)*100,4) if day_close else None,'pnl':round((day_close-buy_px)*shares,2) if day_close else None,'price_source':px.get('price_source', px.get('source')),'close_is_proxy':px.get('close_is_proxy', False)})

    positions = starting_positions + new_positions

    # 3) mark-to-market.
    market_value=0.0; unrealized=0.0
    for pos in positions:
        last=safe_float(pos.get('last_price'), pos.get('entry_price')) or float(pos['entry_price'])
        mv=int(pos['shares'])*last
        market_value += mv
        unrealized += (last-float(pos['entry_price']))*int(pos['shares'])
    nav=cash+market_value
    state={'cash':round(cash,2),'positions':positions,'closed_trades':closed,'updated_at':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    save_state(state)
    return {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'trade_date': date,
        'signal_path': str(sig.get('_signal_path','')),
        'capital': capital,
        'summary': {'buy_order_count': len(sig.get('buy_orders', [])), 'buy_filled_count': len(buys), 'sell_count': len(sells), 'plan_weight': round(total_plan_weight,6), 'fill_weight': round(total_fill_weight,6), 'plan_amount': round(total_plan_amount,2), 'fill_amount': round(total_fill_amount,2), 'cash': round(cash,2), 'market_value': round(market_value,2), 'nav': round(nav,2), 'unrealized_pnl': round(unrealized,2), 'open_positions': len(positions)},
        'sells': sells,
        'rows': rows,
        'positions': positions,
        'historical_review': history_review(date),
        'rules': {'entry':'D1 signal buy_orders by weight, buy_price=open; limit-up bid only checks one-price limit-up fillability', 'exit': 'DI主基线：T+1非涨停直接卖出；涨停持有；跌停/不可卖持有顺延', 'state_path': str(STATE)},
    }



def _signal_buy_order_count(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return len(data.get('buy_orders') or [])
    except Exception:
        return 0


def validate_date(date: str) -> str:
    try:
        if datetime.strptime(date, '%Y-%m-%d').strftime('%Y-%m-%d') != date:
            raise ValueError(date)
    except (ValueError, TypeError):
        raise ValueError(f'无效目标日期: {date!r}，需要 YYYY-MM-DD') from None
    now = datetime.now(timezone(timedelta(hours=8)))
    if date > now.date().isoformat() or (date == now.date().isoformat() and now.time() < time(15, 0)):
        raise ValueError(f'{date}: 目标日尚未收盘（上海时间），拒绝模拟盈亏')
    return date


def validate_signal(path: Path, date: str) -> dict:
    """Reject stale, mismatched or undated snapshots even when path is explicit/audited."""
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'开盘信号必须是 JSON 对象: {path}')
    declared = [data[k] for k in ('trade_date', 'date') if k in data]
    if not declared or any(d != date for d in declared):
        raise ValueError(f'开盘信号日期缺失或与目标 {date} 不符: {path}')
    if not isinstance(data.get('buy_orders'), list):
        raise ValueError(f'开盘信号 buy_orders 缺失或格式错误: {path}')
    return data


def _official_signal_from_audit(date: str) -> Path | None:
    dc = date.replace('-', '')
    audit_dir = ROOT / 'logs' / 'opening_signal_audit'
    audits = sorted(audit_dir.glob(f'd1_recovery_audit_{dc}_*.json'), key=lambda p: p.name)
    if not audits:
        return None
    for audit_path in reversed(audits):
        try:
            audit = json.loads(audit_path.read_text(encoding='utf-8'))
            sig = audit.get('official_signal_path')
            if not sig:
                continue
            p = Path(sig)
            if not p.is_absolute():
                p = ROOT / p
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def latest_signal(date: str) -> Path:
    """Pick the unique official D1_STRONG_CS snapshot for the day.

    优先级：
    1. opening_signal_audit 里登记的 official_signal_path（唯一正式口径）
    2. sent_marker 记录的 signal_path
    3. 当日最新且 buy_orders>0 的 D1 文件
    4. 当日最新 D1 文件
    """
    official = _official_signal_from_audit(date)
    if official:
        return official

    dc=date.replace('-','')
    out_dir = ROOT/'output'/'opening_signal'
    files=sorted([Path(x) for x in glob.glob(str(out_dir/f'd1_strong_cs_signal_{dc}_*.json'))], key=lambda p: p.name)
    if files:
        valid_files=[p for p in files if _signal_buy_order_count(p) > 0]
        if valid_files:
            latest_valid = valid_files[-1]
        else:
            latest_valid = files[-1]
    else:
        latest_valid = None

    marker = out_dir/'sent_markers'/f'd1_sent_{dc}.ok'
    if marker.exists():
        for line in marker.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('signal_path='):
                p=Path(line.split('=',1)[1].strip())
                if not p.is_absolute():
                    p=ROOT/p
                if p.is_file():
                    return p
    if latest_valid:
        return latest_valid
    raise FileNotFoundError(f'未找到 {date} 的 D1_STRONG_CS 开盘信号 JSON')

def history_review(current_date: str) -> dict:
    rows=[]
    latest_by_trade_date = {}
    for p in sorted((OUT).glob('d1_strong_cs_fill_review_*.json')):
        try:
            d=json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        td = d.get('trade_date')
        if not td or td >= current_date:
            continue
        # 同一交易日可能有多次手动重跑文件。
        # 历史基线必须按交易日去重，只取最新一份，否则重复错误文件会污染样本数、成交率和胜率。
        latest_by_trade_date[td] = (p, d)

    for td, (p, d) in sorted(latest_by_trade_date.items()):
        s=d.get('summary', {})
        rows.append({
            'trade_date': d.get('trade_date'),
            'path': str(p),
            'buy_order_count': s.get('buy_order_count', 0),
            'filled_count': s.get('buy_filled_count', s.get('filled_count', 0)),
            'plan_weight': s.get('plan_weight'),
            'fill_weight': s.get('fill_weight'),
            'day_pnl': s.get('unrealized_pnl', s.get('day_pnl')),
            'day_pnl_pct_on_capital': (round(float(s.get('unrealized_pnl')) / 1000000 * 100, 4) if s.get('unrealized_pnl') is not None else s.get('day_pnl_pct_on_capital')),
        })
    pnls=[float(r['day_pnl_pct_on_capital']) for r in rows if r.get('day_pnl_pct_on_capital') is not None]
    fill_rates=[]
    for r in rows:
        b=float(r.get('buy_order_count') or 0)
        if b>0:
            fill_rates.append(float(r.get('filled_count') or 0)/b)
    return {
        'sample_count': len(rows),
        'avg_day_pnl_pct_on_capital': round(mean(pnls),4) if pnls else None,
        'median_day_pnl_pct_on_capital': round(median(pnls),4) if pnls else None,
        'win_rate_pct': round(sum(1 for x in pnls if x>0)/len(pnls)*100,2) if pnls else None,
        'avg_fill_rate_pct': round(mean(fill_rates)*100,2) if fill_rates else None,
        'recent': rows[-10:],
        'baseline_note': '历史统计仅来自当前配置的本地收盘 fill_review 文件，不包含外部长期回测结论。',
    }


def review(date: str, signal_path: Path|None=None):
    validate_date(date)
    sig_path = signal_path or latest_signal(date)
    sig=validate_signal(sig_path, date)
    sig['_signal_path']=str(sig_path)
    return execute_paper_review(date, sig)

def to_md(res):
    s=res['summary']; date=res['trade_date']; h=res.get('historical_review', {})
    rules=res.get('rules', {})
    lines=[
        f'# D1_STRONG_CS 策略模拟复盘 | {date}',
        '',
        f'- 开盘信号: `{res["signal_path"]}`',
        f'- 资金基准: {res["capital"]/10000:.1f}万',
        f'- 买入规则: 按 D1_STRONG_CS buy_orders 权重，买入价=开盘价；挂涨停价口径遇一字板不成交',
        f'- 卖出规则: {rules.get("exit", "SL/TP/MAX_HOLD")}',
        f'- 状态账本: `{rules.get("state_path", "")}`',
        '',
        '## 当日汇总',
        '',
        f'- 计划买入仓位: {s.get("plan_weight",0):.2%} / 计划金额: {s.get("plan_amount",0)/10000:.2f}万',
        f'- 实际模拟买入: {s.get("buy_filled_count",0)}/{s.get("buy_order_count",0)}，成交仓位 {s.get("fill_weight",0):.2%} / 金额 {s.get("fill_amount",0)/10000:.2f}万',
        f'- 当日卖出: {s.get("sell_count",0)} 笔',
        f'- 当前持仓: {s.get("open_positions",0)} 只，持仓市值 {s.get("market_value",0)/10000:.2f}万，现金 {s.get("cash",0)/10000:.2f}万',
        f'- 账户 NAV: {s.get("nav",0)/10000:.2f}万，未实现盈亏 {s.get("unrealized_pnl",0)/10000:.2f}万',
        '',
        '## 买入明细',
        '',
    ]
    for r in res.get('rows', []):
        flag='✅成交' if r.get('filled') else '❌未成交'
        lines.append(f"- {flag} R{r.get('rank')} {r.get('code')} {r.get('name')} 计划{float(r.get('weight') or 0):.1%}")
        if r.get('filled'):
            lines.append(f"  - 开盘/买入价: {r.get('open')} / {r.get('buy_price')} | 收盘: {r.get('close')} | 股数: {r.get('shares')} | 当日: {r.get('day_ret_pct')}% | 浮盈亏: {r.get('pnl')}")
        else:
            lines.append(f"  - 原因: {r.get('fill_reason')}")
    lines += ['', '## 卖出明细', '']
    if res.get('sells'):
        for r in res['sells']:
            lines.append(f"- ✅卖出 {r.get('code')} {r.get('name')} | 类型 {r.get('exit_type')} | 买入 {r.get('entry_price')} / 卖出 {r.get('exit_price')} | 股数 {r.get('shares')} | 收益 {r.get('ret_pct')}% | PnL {r.get('pnl')}")
    else:
        lines.append('- 今日无触发卖出。')
    lines += ['', '## 当前持仓', '']
    if res.get('positions'):
        for p in res['positions']:
            last=float(p.get('last_price') or p.get('entry_price') or 0)
            entry=float(p.get('entry_price') or 0)
            ret=(last/entry-1)*100 if entry else 0
            source = p.get('last_price_source', '')
            proxy = ' | ⚠️收盘代理价' if p.get('last_close_is_proxy') else ''
            source_text = f' | 价格源 {source}' if source else ''
            lines.append(f"- {p.get('code')} {p.get('name')} | 入场日 {p.get('entry_date')} | 成本 {entry:.4f} | 最新/收盘 {last:.4f} | 股数 {p.get('shares')} | 持有 {p.get('hold_days')}天 | 浮动 {ret:.2f}%{source_text}{proxy}")
    else:
        lines.append('- 当前无持仓。')
    lines += ['', '## 过往历史回顾', '']
    lines.append(f"- 本地收盘回顾样本数: {h.get('sample_count', 0)}")
    if h.get('sample_count'):
        lines.append(f"- 平均当日浮盈亏/资金: {h.get('avg_day_pnl_pct_on_capital')}%")
        lines.append(f"- 中位数当日浮盈亏/资金: {h.get('median_day_pnl_pct_on_capital')}%")
        lines.append(f"- 当日胜率: {h.get('win_rate_pct')}%")
        lines.append(f"- 平均成交率: {h.get('avg_fill_rate_pct')}%")
    lines.append(f"- 长周期基线备注: {h.get('baseline_note')}")
    return '\n'.join(lines)+'\n'

def main():
    ap=argparse.ArgumentParser(description='D1_STRONG_CS 收盘模拟成交回顾')
    ap.add_argument('--date', required=True, help='YYYY-MM-DD')
    ap.add_argument('--signal', default=None, help='指定开盘信号json')
    args=ap.parse_args()
    res=review(args.date, Path(args.signal) if args.signal else None)
    OUT.mkdir(parents=True, exist_ok=True)
    dc=args.date.replace('-','')
    ts=datetime.now().strftime('%Y%m%d_%H%M%S')
    jp=OUT/f'd1_strong_cs_fill_review_{dc}_{ts}.json'
    mp=OUT/f'd1_strong_cs_fill_review_{dc}_{ts}.md'
    jp.write_text(json.dumps(res,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    mp.write_text(to_md(res),encoding='utf-8')
    print(json.dumps(res['summary'],ensure_ascii=False))
    print('JSON', jp)
    print('MD', mp)

if __name__=='__main__':
    main()
