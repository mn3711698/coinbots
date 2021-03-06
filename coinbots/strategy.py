# -*- coding: utf-8 -*-
import logging
import asyncio
from .utils import dotdict
from .streaming import Streaming
from .ohlcvbuilder import OHLCVBuilder
from .inventory import Inventory
from .exchange import Exchange, ExchangeError
from .board import Board
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone
from time import time

class Strategy:

    def __init__(self, yourlogic=None, interval=60):

        # トレーディングロジック設定
        self.yourlogic = yourlogic

        # 設定
        self.settings = dotdict()
        self.settings.apiKey = ''
        self.settings.secret = ''
        self.settings.symbol = 'BTC/JPY'

        # 動作タイミング
        self.settings.interval = interval
        self.settings.minimum_interval = 0

        # OHLCV設定
        self.settings.max_ohlcv_size = 1000
        self.settings.disable_rich_ohlcv = False

        # その他設定
        self.settings.enable_board = False
        self.settings.enable_board_api = False

        # ログ設定
        self.logger = logging.getLogger(__name__)

    async def start(self):
        self.logger.info('Start Trading')

        # ペア情報
        self.pair = self.settings.symbol.replace('/','_').lower()
        self.spec = Exchange.ProductSpecs[self.pair]

        # APIセットアップ
        self.api = Exchange(self.settings.apiKey, self.settings.secret)

        # ストリーム配信
        self.streaming = Streaming(Streaming.SocketioSource())
        self.executions_ep = await self.streaming.get_trades_endpoint(self.pair, 5000)

        # OHLCVビルダー設定
        self.ohlcvbuilder = OHLCVBuilder(
            maxlen=self.settings.max_ohlcv_size,
            disable_rich_ohlcv=self.settings.disable_rich_ohlcv)

        # 注文管理
        self.inventory = Inventory(self.spec)
        self.latest_trade_id = None

        # 板情報（配信）
        if self.settings.enable_board:
            self.board = Board(self.pair)
            ob = await self.api.orderbooks(self.pair)
            self.board.sync(ob)
            await self.board.attach(self.streaming)
        # 板情報（API）
        elif self.settings.enable_board_api:
            self.board = Board(self.pair)

        # ロジック実行
        await asyncio.wait([
            self.balance_polling(),
            self.cancel_nonactive_orders(),
            self.standard_logic(),
            self.inventory.start(),
            self.streaming.start()])

    def get_order(self, myid):
        return self.inventory.get_order(myid)

    async def order(self, myid, side, size, limit=None, cancel_after_seconds=None, limit_mask=0):
        # 注文がオープンならキャンセル
        o = self.inventory.get_order(myid)
        if o['status'] in Inventory.OPEN_STATUS:
            if abs(o['rate']-limit)>limit_mask or abs(o['amount']-size)>0:
                try:
                    self.logger.info('CANCEL {myid} {status} {order_type} {rate} {executed_amount}/{amount} {id}'.format(**o))
                    await self.api.cancel(o)
                except ExchangeError as e:
                    self.logger.warning(type(e).__name__ + ": {0}".format(e))
            else:
                # 価格・サイズが同じなら注文しない
                return

        # 新規注文
        try:
            res = await self.api.order(self.pair,side,size,limit)
            self.inventory.new_order(myid,res)
            o = self.inventory.get_order(myid)
            self.logger.info('NEW {myid} {status} {order_type} {rate} {executed_amount}/{amount} {id}'.format(**o))
            # 後でキャンセル
            if cancel_after_seconds is not None:
                asyncio.ensure_future(self._cancel_later(o,cancel_after_seconds))
        except ExchangeError as e:
            self.logger.warning(type(e).__name__ + ": {0}".format(e))

    async def _cancel_later(self, o, seconds):
        try:
            await asyncio.sleep(seconds)
            if o['status'] in Inventory.OPEN_STATUS:
                self.logger.info('CANCEL LATER {myid} {status} {order_type} {rate} {executed_amount}/{amount} {id}'.format(**o))
                await self.api.cancel(o)
        except ExchangeError as e:
            self.logger.warning(type(e).__name__ + ": {0}".format(e))
        except Exception as e:
            self.logger.exception(e)

    async def cancel(self, myid):
        # 注文がオープンならキャンセル
        o = self.inventory.get_order(myid)
        if o['status'] in Inventory.OPEN_STATUS:
            try:
                self.logger.info('CANCEL {myid} {status} {order_type} {rate} {executed_amount}/{amount} {id}'.format(**o))
                await self.api.cancel(o)
            except ExchangeError as e:
                self.logger.warning(type(e).__name__ + ": {0}".format(e))

    async def cancel_order_all(self):
        pass

    async def check_balance(self):
        try:
            ticker = await self.api.ticker(pair=self.pair)
            balance = await self.api.balance()
            btc = balance['btc']+balance['btc_reserved']
            jpy = balance['jpy']+balance['jpy_reserved']
            total = int(jpy + ticker['bid']*btc)
            balance['jpy_total'] = total
            self.balance = dotdict(balance)
            self.logger.info(f'btc {btc:.8f} jpy {jpy:.0f} total {total}')
        except ExchangeError as e:
            self.logger.warning(type(e).__name__ + ": {0}".format(e))

    async def check_trades(self):
        try:
            trades = await self.api.get_my_trades(end=self.latest_trade_id)
            if len(trades):
                self.latest_trade_id = max(t['id'] for t in trades)
            self.inventory.check_my_trades(trades)
        except ExchangeError as e:
            self.logger.warning(type(e).__name__ + ": {0}".format(e))

    async def balance_polling(self):
        while True:
            await asyncio.sleep(300)
            try:
                # 定期的に資産情報取得
                await self.check_balance()
            except Exception as e:
                self.logger.exception(e)

    async def cancel_nonactive_orders(self):
        while True:
            await asyncio.sleep(15)
            try:
                # 最近の注文情報取得
                recent_orders = self.inventory.get_nonactive_orders()
                if len(recent_orders):
                    # 未決済の注文取得
                    open_orders = await self.api.get_orders()
                    for o in open_orders:
                        self.logger.info('{id} {order_type} {rate} {pair} {pending_amount}'.format(**o))
                    open_orders = {o['id']:o for o in open_orders}
                    # アクティブ注文を全てキャンセル
                    cancel_needed = [o for o in recent_orders if o['id'] in open_orders]
                    if len(cancel_needed):
                        for o in cancel_needed:
                            try:
                                self.logger.info('FORCED CANCEL {myid} {status} {order_type} {rate} {executed_amount}/{amount} {id}'.format(**o))
                                await self.api.cancel(o)
                            except ExchangeError as e:
                                self.logger.warning(type(e).__name__ + ": {0}".format(e))

            except ExchangeError as e:
                self.logger.warning(type(e).__name__ + ": {0}".format(e))
            except Exception as e:
                self.logger.exception(e)

    async def standard_logic(self):
        try:
            await self.check_balance()
            await self.executions_ep.wait()
        except Exception as e:
            self.logger.exception(e)
        last_entry_time = time()
        while True:
            try:
                # 待ち
                if self.settings.interval:
                    await asyncio.sleep((-time() % self.settings.interval) or self.settings.interval)
                else:
                    await self.executions_ep.wait()

                # 最小インターバル
                if self.settings.minimum_interval:
                    t1 = last_entry_time // self.settings.minimum_interval
                    t2 = time() // self.settings.minimum_interval
                    can_entry = t2 > t1
                else:
                    can_entry = True

                # 注文情報更新
                if can_entry:
                    if self.settings.enable_board_api:
                        ob = await self.api.orderbooks(self.pair)
                        self.board.sync(ob)
                    await self.check_trades()

                # ポジション情報コピー
                self.long_size = self.inventory.position.long_size
                self.short_size = self.inventory.position.short_size
                self.position_size = self.inventory.position.position_size
                self.position_avg_price = self.inventory.position.position_avg_price

                # 約定履歴取得
                executions = await self.executions_ep.get_data()
                ohlcv = self.ohlcvbuilder.create_boundary_ohlcv(executions)

                # ロジックコール
                if can_entry:
                    last_entry_time = time()
                    if self.settings.enable_board:
                        board = self.board
                        bids, asks = board.sort()
                        bid, ask = bids[0]['price'], asks[0]['price']
                        spread = ask - bid
                        if spread<-50:
                            self.logger.warning(f'orderbooks needs to be sync. spr {spread} bid {bid} ask {ask}')
                            ob = await self.api.orderbooks(self.pair)
                            board.sync(ob)
                    elif self.settings.enable_board_api:
                        board = self.board
                    else:
                        board = None
                    await self.yourlogic(
                        executions=executions,
                        ohlcv=ohlcv,
                        board=board,
                        strategy=self)
            except Exception as e:
                self.logger.exception(e)
