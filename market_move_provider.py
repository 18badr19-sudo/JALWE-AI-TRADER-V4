"""Read-only, paced Alpaca HTTP transport for independent historical studies."""
from datetime import datetime, timedelta, timezone
import time

import pandas as pd
import requests

from market_move_research import NY, utc


class ResearchReadError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class MoveProvider:
    def __init__(self, key, secret, *, session=None, interval=2.0):
        if not key or not secret:
            raise ResearchReadError('CREDENTIALS_UNAVAILABLE')
        self.session = session or requests.Session()
        self.headers = {'APCA-API-KEY-ID':key, 'APCA-API-SECRET-KEY':secret}
        self.interval = max(float(interval), .5)
        self.last = 0

    def get(self, url, params=None):
        # The allowed read endpoints are hardcoded here. No POST/DELETE/PUT API exists.
        allowed = {'https://paper-api.alpaca.markets/v2/assets',
                   'https://paper-api.alpaca.markets/v2/calendar',
                   'https://data.alpaca.markets/v2/stocks/bars',
                   'https://data.alpaca.markets/v2/stocks/trades',
                   'https://data.alpaca.markets/v1beta1/news'}
        if url not in allowed:
            raise ResearchReadError('ENDPOINT_NOT_ALLOWED')
        for attempt in range(3):
            time.sleep(max(0, self.interval-(time.monotonic()-self.last)))
            self.last = time.monotonic()
            try:
                response = self.session.get(url, params=params, headers=self.headers, timeout=15)
                if response.status_code in {429,500,502,503,504} and attempt < 2:
                    time.sleep(min(10, 2**attempt))
                    continue
                if response.status_code != 200:
                    raise ResearchReadError(f'HTTP_{response.status_code}', status_code=response.status_code)
                return response.json()
            except requests.RequestException:
                if attempt == 2:
                    raise ResearchReadError('PROVIDER_READ_FAILED') from None
        raise ResearchReadError('PROVIDER_READ_FAILED')

    def assets(self):
        rows = self.get('https://paper-api.alpaca.markets/v2/assets', {'status':'active','asset_class':'us_equity'})
        if not isinstance(rows, list):
            raise ResearchReadError('INVALID_ASSET_RESPONSE')
        symbols = set()
        for row in rows:
            if (isinstance(row, dict) and row.get('status') == 'active'
                    and row.get('class',row.get('asset_class')) == 'us_equity'):
                # Catalog research does not submit orders. A historical move
                # remains worth studying when this broker cannot trade it.
                symbol = str(row.get('symbol') or '').strip().upper()
                if symbol and all(c.isalpha() or c in '.-' for c in symbol) and len(symbol) <= 12:
                    symbols.add(symbol)
        if not symbols:
            raise ResearchReadError('NO_ELIGIBLE_ASSETS')
        return sorted(symbols)

    def calendar(self, now):
        day = now.astimezone(NY).date()
        rows = self.get('https://paper-api.alpaca.markets/v2/calendar',
                        {'start':(day-timedelta(days=45)).isoformat(),'end':(day+timedelta(days=10)).isoformat()})
        if not isinstance(rows, list) or not rows:
            raise ResearchReadError('CALENDAR_UNAVAILABLE')
        sessions = []
        for row in rows:
            if not isinstance(row, dict):
                raise ResearchReadError('INVALID_CALENDAR')
            def at(name):
                return datetime.fromisoformat(row['date']+'T'+row[name]).replace(tzinfo=NY).astimezone(timezone.utc)
            opening, close = at('open'), at('close')
            if opening >= close:
                raise ResearchReadError('INVALID_CALENDAR')
            sessions.append(dict(date=row['date'],open=opening.isoformat(),close=close.isoformat()))
        return sorted(sessions, key=lambda r:r['date'])

    def bars(self, symbols, start, end, timeframe, *, max_pages=10):
        result, token, seen = {}, None, set()
        for _ in range(max_pages):
            page = self.get('https://data.alpaca.markets/v2/stocks/bars',
                dict(symbols=','.join(symbols),start=start.isoformat(),end=end.isoformat(),timeframe=timeframe,
                     adjustment='split',feed='sip',sort='asc',limit=10000,page_token=token))
            rows = page.get('bars') if isinstance(page, dict) else None
            if not isinstance(rows, dict):
                raise ResearchReadError('INVALID_BARS_RESPONSE')
            for symbol, data in rows.items():
                if symbol not in symbols or not isinstance(data, list):
                    raise ResearchReadError('INVALID_BARS_RESPONSE')
                result.setdefault(symbol, []).extend(data)
            token = page.get('next_page_token')
            if not token:
                return result
            if not isinstance(token, str) or token in seen:
                raise ResearchReadError('INVALID_PAGINATION')
            seen.add(token)
        raise ResearchReadError('INCOMPLETE_PAGINATION')

    def daily(self, symbols, sessions, now):
        # End before the last completed session's delayed historical availability.
        end = utc(sessions[-1]['close'])
        if now < end+timedelta(minutes=16):
            raise ResearchReadError('WAITING_FOR_HISTORICAL_DELAY')
        start = utc(sessions[0]['open']).astimezone(NY).replace(hour=0,minute=0,second=0).astimezone(timezone.utc)
        # A daily bar timestamp is session midnight; avoid including an unfinished future day.
        final_end = end
        all_rows = {}
        for index in range(0, len(symbols), 500):
            batch = symbols[index:index+500]
            all_rows.update(self.bars(batch,start,final_end,'1Day'))
        return all_rows

    def intraday(self, symbol, session, now):
        end = utc(session['close'])
        if now < end+timedelta(minutes=16):
            raise ResearchReadError('WAITING_FOR_HISTORICAL_DELAY')
        rows = self.bars([symbol],utc(session['open']),end,'1Min').get(symbol, [])
        return self._minute_frame(rows)

    @staticmethod
    def _minute_frame(rows):
        data = pd.DataFrame(rows)
        if data.empty:
            return pd.DataFrame(columns=['open','high','low','close','volume'],index=pd.DatetimeIndex([],tz='UTC'))
        data = data.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})
        data.index = pd.to_datetime(data.pop('t'),utc=True,errors='coerce')
        return data

    def intraday_many(self, symbols, session, now):
        """One bounded, fully paginated SIP read; absent symbols stay empty."""
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return {}
        if len(symbols) > 24:
            raise ResearchReadError('INTRADAY_BATCH_TOO_LARGE')
        end = utc(session['close'])
        if now < end+timedelta(minutes=16):
            raise ResearchReadError('WAITING_FOR_HISTORICAL_DELAY')
        rows = self.bars(symbols,utc(session['open']),end,'1Min')
        # bars() follows every token or raises; never treat the first page as
        # proof that symbols sorted after it have no trades.
        return {symbol:self._minute_frame(rows.get(symbol,[])) for symbol in symbols}

    def news_before(self, symbol, cutoff):
        articles, token, seen, complete, malformed = [], None, set(), False, 0
        start = cutoff-timedelta(days=1)
        for _ in range(2):
            page = self.get('https://data.alpaca.markets/v1beta1/news',
                dict(symbols=symbol,start=start.isoformat(),end=cutoff.isoformat(),
                     sort='asc',limit=50,include_content=False,page_token=token))
            if not isinstance(page,dict) or not isinstance(page.get('news'),list):
                raise ResearchReadError('INVALID_NEWS_RESPONSE')
            for row in page['news']:
                try:
                    created, updated = utc(row['created_at']), utc(row['updated_at'])
                    if not start <= created <= updated <= cutoff or symbol not in row.get('symbols',[]):
                        continue
                    headline = str(row.get('headline') or '').lower()
                    keywords = [k for k in ('earnings','fda','merger','acquisition','offering','contract','guidance') if k in headline]
                    articles.append(dict(id=row['id'],created_at=created.isoformat(),
                        updated_at=updated.isoformat(),keywords=keywords,source=row.get('source')))
                except (KeyError,ValueError,TypeError):
                    malformed += 1
                    continue
            token = page.get('next_page_token')
            if not token:
                complete = True
                break
            if not isinstance(token,str) or token in seen:
                break
            seen.add(token)
        return dict(status='AVAILABLE' if complete and not malformed else 'INCOMPLETE',cutoff=cutoff.isoformat(),
                    malformed_articles=malformed,
                    count=len(articles),articles=articles[:5],historical_updated_content_filtered=True)

    def gap_trades(self, symbol, minute, now):
        from market.gap_evidence import probe_minute
        if now < minute+timedelta(minutes=17):
            raise ResearchReadError('WAITING_FOR_HISTORICAL_DELAY')
        return probe_minute(symbol, minute, 'sip',
            lambda params: self.get('https://data.alpaca.markets/v2/stocks/trades', params), max_pages=2)
