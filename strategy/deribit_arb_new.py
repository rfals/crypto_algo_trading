# -*- coding: utf-8 -*-

from crypto_trading.service.base import ServiceState, ServiceBase, start_service
from crypto_trading.config import *
import zmq.asyncio
import asyncio
import json
import time



margin = [0, 0, 0]
future = None
future_size = 0
perpetual = None
perpetual_size = 0


def get_expiration(sym):
    ex = sym.split('-')[1]
    return ex.replace(ex[2:5], {'JAN':'01','FEB':'02','MAR':'03','APR':'04','MAY':'05','JUN':'06',
                                'JUL':'07','AUG':'08','SEP':'09','OCT':'10','NOV':'11','DEC':'12'}[ex[2:5]])

expiration = time.mktime(time.strptime(get_expiration(SEASON_FUTURE), '%d%m%y')) + 3600*8


class OrderState():
    def __init__(self, if_placed=False, if_changing=False, if_cancelling=False,  order={}):
        self.if_placed = if_placed
        self.if_changing = if_changing
        self.if_cancelling = if_cancelling
        self.order = order

    def reset(self):
        self.if_placed = False
        self.if_changing = False
        self.if_cancelling = False
        self.order = {}

f_limit_order = OrderState()
p_limit_order = OrderState()


class Quote():
    def __init__(self, bid, bidsize, ask, asksize, index_price):
        self.bid = bid
        self.bidsize = bidsize
        self.ask = ask
        self.asksize = asksize
        self.index_price = index_price


class FutureArbitrage(ServiceBase):
    
    def __init__(self, logger_name):
        ServiceBase.__init__(self, logger_name)
        # subscribe market data
        self.deribitmd = self.ctx.socket(zmq.SUB)
        self.deribitmd.connect('tcp://localhost:9050')
        self.deribitmd.setsockopt_string(zmq.SUBSCRIBE, '')
        # request client for transaction
        self.deribittdreq = self.ctx.socket(zmq.REQ)
        self.deribittdreq.connect('tcp://localhost:9020')
        # subscribe transaction data
        self.deribittd = self.ctx.socket(zmq.SUB)
        self.deribittd.connect('tcp://localhost:9010')
        self.deribittd.setsockopt_string(zmq.SUBSCRIBE, '')

        # async queue to sequentially combine market data and tx data
        self.msg = asyncio.Queue()

    # find gap between perpetual and current season future, and make transaction when conditions are satisfied
    async def find_quotes_gap(self):
        try:
            global future, future_size, f_limit_order, perpetual, perpetual_size, p_limit_order, margin
            pos_idx = sum([1 if max(abs(future_size), abs(perpetual_size)) >= i else 0 for i in POSITION_SIZE_THRESHOLD])
            min_left = (expiration - time.time())/60
            if (future.bid + future.ask)/2 >= future.index_price:
                premium = ((future.bid + future.ask)/2 - max(future.index_price, (perpetual.bid + perpetual.ask)/2))/future.index_price * (525600/min_left) * 100
            else:
                premium = (min(future.index_price, (perpetual.bid + perpetual.ask)/2) - (future.bid + future.ask)/2)/future.index_price * (525600/min_left) * 100

            # future > perpetual situation
            if all((min(future.bid-perpetual.bid, future.ask-perpetual.ask) >= TX_ENTRY_PRICE_GAP/100*future.index_price,
                    premium >= TX_ENTRY_GAP[pos_idx])):
                if not f_limit_order.if_placed:
                    self.logger.info('**** entry premium: {}, future limit ****'.format(premium))
                    await self.deribittdreq.send_string(json.dumps({
                        'accountid': DERIBIT_ACCOUNT_ID, 'method': 'sell',
                        'params': {'instrument_name': SEASON_FUTURE,
                                   'amount': min(SIZE_PER_TRADE, perpetual.asksize),
                                   'type': 'limit',
                                   'price': future.ask - MINIMUM_TICK_SIZE,
                                   'post_only': True, }
                    }))
                    await self.deribittdreq.recv_string()
                    f_limit_order.if_placed = True
                else:
                    if f_limit_order.order:
                        if not f_limit_order.order['order_state'] in ('filled', 'cancelled'):
                            if future.ask < f_limit_order.order['price'] and not f_limit_order.if_changing:
                                self.logger.info('**** entry change price to: {}, future limit ****'.format(future.ask - MINIMUM_TICK_SIZE))
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID, 'method': 'edit',
                                    'params': {'order_id': f_limit_order.order['order_id'],
                                               'amount': min(SIZE_PER_TRADE, perpetual.asksize),
                                               'price': future.ask - MINIMUM_TICK_SIZE,
                                               'post_only': True, }
                                }))
                                await self.deribittdreq.recv_string()
                                f_limit_order.if_changing = True
                        else:
                            f_limit_order.reset()
                if not p_limit_order.if_placed:
                    self.logger.info('**** entry premium: {}, perpetual limit ****'.format(premium))
                    await self.deribittdreq.send_string(json.dumps({
                        'accountid': DERIBIT_ACCOUNT_ID, 'method': 'buy',
                        'params': {'instrument_name': PERPETUAL,
                                   'amount': min(SIZE_PER_TRADE, future.bidsize),
                                   'type': 'limit',
                                   'price': perpetual.bid + MINIMUM_TICK_SIZE,
                                   'post_only': True, }
                    }))
                    await self.deribittdreq.recv_string()
                    p_limit_order.if_placed = True
                else:
                    if p_limit_order.order:
                        if not p_limit_order.order['order_state'] in ('filled', 'cancelled'):
                            if perpetual.bid > p_limit_order.order['price'] and not p_limit_order.if_changing:
                                self.logger.info('**** entry change price to: {}, perpetual limit ****'.format(perpetual.bid + MINIMUM_TICK_SIZE))
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID, 'method': 'edit',
                                    'params': {'order_id': p_limit_order.order['order_id'],
                                               'amount': min(SIZE_PER_TRADE, future.bidsize),
                                               'price': perpetual.bid + MINIMUM_TICK_SIZE,
                                               'post_only': True, }
                                }))
                                await self.deribittdreq.recv_string()
                                p_limit_order.if_changing = True
                        else:
                            p_limit_order.reset()
            # perpetual > future situation
            elif all((min(perpetual.bid-future.bid, perpetual.ask-future.ask) >= TX_ENTRY_PRICE_GAP/100*future.index_price,
                      premium <= - TX_ENTRY_GAP[pos_idx])):
                if not f_limit_order.if_placed:
                    self.logger.info('**** reverse entry premium: {}, future limit ****'.format(premium))
                    await self.deribittdreq.send_string(json.dumps({
                        'accountid': DERIBIT_ACCOUNT_ID, 'method': 'buy',
                        'params': {'instrument_name': SEASON_FUTURE,
                                   'amount': min(SIZE_PER_TRADE, perpetual.bidsize),
                                   'type': 'limit',
                                   'price': future.bid + MINIMUM_TICK_SIZE,
                                   'post_only': True, }
                    }))
                    await self.deribittdreq.recv_string()
                    f_limit_order.if_placed = True
                else:
                    if f_limit_order.order:
                        if not f_limit_order.order['order_state'] in ('filled', 'cancelled'):
                            if future.bid > f_limit_order.order['price'] and not f_limit_order.if_changing:
                                self.logger.info('**** reverse entry change price to: {}, future limit ****'.format(future.bid + MINIMUM_TICK_SIZE))
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID, 'method': 'edit',
                                    'params': {'order_id': f_limit_order.order['order_id'],
                                               'amount': min(SIZE_PER_TRADE, perpetual.bidsize),
                                               'price': future.bid + MINIMUM_TICK_SIZE,
                                               'post_only': True, }
                                }))
                                await self.deribittdreq.recv_string()
                                f_limit_order.if_changing = True
                        else:
                            f_limit_order.reset()
                if not p_limit_order.if_placed:
                    self.logger.info('**** reverse entry premium: {}, perpetual limit ****'.format(premium))
                    await self.deribittdreq.send_string(json.dumps({
                        'accountid': DERIBIT_ACCOUNT_ID, 'method': 'sell',
                        'params': {'instrument_name': PERPETUAL,
                                   'amount': min(SIZE_PER_TRADE, future.asksize),
                                   'type': 'limit',
                                   'price': perpetual.ask - MINIMUM_TICK_SIZE,
                                   'post_only': True, }
                    }))
                    await self.deribittdreq.recv_string()
                    p_limit_order.if_placed = True
                else:
                    if p_limit_order.order:
                        if not p_limit_order.order['order_state'] in ('filled', 'cancelled'):
                            if perpetual.ask < p_limit_order.order['price'] and not p_limit_order.if_changing:
                                self.logger.info('**** reverse entry change price to: {}, perpetual limit ****'.format(perpetual.ask - MINIMUM_TICK_SIZE))
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID, 'method': 'edit',
                                    'params': {'order_id': p_limit_order.order['order_id'],
                                               'amount': min(SIZE_PER_TRADE, future.asksize),
                                               'price': perpetual.ask - MINIMUM_TICK_SIZE,
                                               'post_only': True, }
                                }))
                                await self.deribittdreq.recv_string()
                                p_limit_order.if_changing = True
                        else:
                            p_limit_order.reset()
            # close position when gap disppears in case of shorting future and longing perpetual
            elif premium <= TX_EXIT_GAP and future_size < 0 and perpetual_size > 0:
                if not f_limit_order.if_placed:
                    self.logger.info('**** exit premium: {}, future limit ****'.format(premium))
                    await self.deribittdreq.send_string(json.dumps({
                        'accountid': DERIBIT_ACCOUNT_ID, 'method': 'buy',
                        'params': {'instrument_name': SEASON_FUTURE,
                                   'amount': min(SIZE_PER_TRADE, perpetual.bidsize, abs(future_size)),
                                   'type': 'limit',
                                   'price': future.bid + MINIMUM_TICK_SIZE,
                                   'post_only': True, }
                    }))
                    await self.deribittdreq.recv_string()
                    f_limit_order.if_placed = True
                else:
                    if f_limit_order.order:
                        if not f_limit_order.order['order_state'] in ('filled', 'cancelled'):
                            if future.bid > f_limit_order.order['price'] and not f_limit_order.if_changing:
                                self.logger.info('**** exit change price to: {}, future limit ****'.format(future.bid + MINIMUM_TICK_SIZE))
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID, 'method': 'edit',
                                    'params': {'order_id': f_limit_order.order['order_id'],
                                               'amount': min(SIZE_PER_TRADE, perpetual.bidsize, abs(future_size)),
                                               'price': future.bid + MINIMUM_TICK_SIZE,
                                               'post_only': True, }
                                }))
                                await self.deribittdreq.recv_string()
                                f_limit_order.if_changing = True
                        else:
                            f_limit_order.reset()
                if not p_limit_order.if_placed:
                    self.logger.info('**** exit premium: {}, perpetual limit ****'.format(premium))
                    await self.deribittdreq.send_string(json.dumps({
                        'accountid': DERIBIT_ACCOUNT_ID, 'method': 'sell',
                        'params': {'instrument_name': PERPETUAL,
                                   'amount': min(SIZE_PER_TRADE, future.asksize, abs(perpetual_size)),
                                   'type': 'limit',
                                   'price': perpetual.ask - MINIMUM_TICK_SIZE,
                                   'post_only': True, }
                    }))
                    await self.deribittdreq.recv_string()
                    p_limit_order.if_placed = True
                else:
                    if p_limit_order.order:
                        if not p_limit_order.order['order_state'] in ('filled', 'cancelled'):
                            if perpetual.ask < p_limit_order.order['price'] and not p_limit_order.if_changing:
                                self.logger.info('**** exit change price to: {}, perpetual limit ****'.format(perpetual.ask - MINIMUM_TICK_SIZE))
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID, 'method': 'edit',
                                    'params': {'order_id': p_limit_order.order['order_id'],
                                               'amount': min(SIZE_PER_TRADE, future.asksize, abs(perpetual_size)),
                                               'price': perpetual.ask - MINIMUM_TICK_SIZE,
                                               'post_only': True, }
                                }))
                                await self.deribittdreq.recv_string()
                                p_limit_order.if_changing = True
                        else:
                            p_limit_order.reset()
            # close position when gap disppears in case of longing future and shorting perpetual
            elif premium >= - TX_EXIT_GAP and future_size > 0 and perpetual_size < 0:
                if not f_limit_order.if_placed:
                    self.logger.info('**** exit premium: {}, future limit ****'.format(premium))
                    await self.deribittdreq.send_string(json.dumps({
                        'accountid': DERIBIT_ACCOUNT_ID, 'method': 'sell',
                        'params': {'instrument_name': SEASON_FUTURE,
                                   'amount': min(SIZE_PER_TRADE, perpetual.asksize, abs(future_size)),
                                   'type': 'limit',
                                   'price': future.ask - MINIMUM_TICK_SIZE,
                                   'post_only': True, }
                    }))
                    await self.deribittdreq.recv_string()
                    f_limit_order.if_placed = True
                else:
                    if f_limit_order.order:
                        if not f_limit_order.order['order_state'] in ('filled', 'cancelled'):
                            if future.ask < f_limit_order.order['price'] and not f_limit_order.if_changing:
                                self.logger.info('**** exit change price to: {}, future limit ****'.format(future.ask - MINIMUM_TICK_SIZE))
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID, 'method': 'edit',
                                    'params': {'order_id': f_limit_order.order['order_id'],
                                               'amount': min(SIZE_PER_TRADE, perpetual.asksize, abs(future_size)),
                                               'price': future.ask - MINIMUM_TICK_SIZE,
                                               'post_only': True, }
                                }))
                                await self.deribittdreq.recv_string()
                                f_limit_order.if_changing = True
                        else:
                            f_limit_order.reset()
                if not p_limit_order.if_placed:
                    self.logger.info('**** exit premium: {}, perpetual limit ****'.format(premium))
                    await self.deribittdreq.send_string(json.dumps({
                        'accountid': DERIBIT_ACCOUNT_ID, 'method': 'buy',
                        'params': {'instrument_name': PERPETUAL,
                                   'amount': min(SIZE_PER_TRADE, future.asksize, abs(perpetual_size)),
                                   'type': 'limit',
                                   'price': perpetual.bid + MINIMUM_TICK_SIZE,
                                   'post_only': True, }
                    }))
                    await self.deribittdreq.recv_string()
                    p_limit_order.if_placed = True
                else:
                    if p_limit_order.order:
                        if not p_limit_order.order['order_state'] in ('filled', 'cancelled'):
                            if perpetual.bid > p_limit_order.order['price'] and not p_limit_order.if_changing:
                                self.logger.info('**** exit change price to: {}, perpetual limit ****'.format(perpetual.bid + MINIMUM_TICK_SIZE))
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID, 'method': 'edit',
                                    'params': {'order_id': p_limit_order.order['order_id'],
                                               'amount': min(SIZE_PER_TRADE, future.bidsize, abs(perpetual_size)),
                                               'price': perpetual.bid + MINIMUM_TICK_SIZE,
                                               'post_only': True, }
                                }))
                                await self.deribittdreq.recv_string()
                                p_limit_order.if_changing = True
                        else:
                            p_limit_order.reset()
            else:
                if f_limit_order.if_placed or p_limit_order.if_placed:
                    if not f_limit_order.if_cancelling or not p_limit_order.if_cancelling:
                        self.logger.info('**** gap disppear, cancel_all ****')
                        await self.deribittdreq.send_string(json.dumps({
                            'accountid': DERIBIT_ACCOUNT_ID, 'method': 'cancel_all', 'params': {}
                        }))
                        await self.deribittdreq.recv_string()
                        f_limit_order.if_cancelling = True
                        p_limit_order.if_cancelling = True
        except AttributeError:
            pass
        except Exception as e:
            self.logger.exception(e)

    async def process_msg(self):
        try:
            global future, future_size, perpetual, perpetual_size, margin, f_limit_order, p_limit_order
            while self.state == ServiceState.started:
                msg = await self.msg.get()
                if msg['type'] not in ('quote', 'user.portfolio', 'buy', 'sell', 'edit'):
                    self.logger.info('---- td res: {}, {}'.format(msg['type'], msg['data']))
                    
                if msg['type'] == 'quote':
                    d = msg['data']
                    if d['instrument_name'] == PERPETUAL:
                        perpetual = Quote(d['best_bid_price'], d['best_bid_amount'], d['best_ask_price'], d['best_ask_amount'], d['index_price'])
                    elif d['instrument_name'] == SEASON_FUTURE:
                        future = Quote(d['best_bid_price'], d['best_bid_amount'], d['best_ask_price'], d['best_ask_amount'], d['index_price'])
                    await self.find_quotes_gap()
                elif msg['type'] == 'user.changes.future':
                    changes = msg['data']
                    if changes['instrument_name'] == SEASON_FUTURE:
                        if changes['trades']:
                            filled = sum([tx['amount'] if tx['order_type'] == 'limit' else 0 for tx in changes['trades']])
                            if filled > 0:
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID,
                                    'method': 'buy' if changes['trades'][0]['direction'] == 'sell' else 'sell',
                                    'params': {'instrument_name': PERPETUAL, 'amount': filled, 'type': 'market',}
                                }))
                                await self.deribittdreq.recv_string()
                        if changes['positions']:
                            future_size = changes['positions'][0]['size']
                        if changes['orders']:
                            for order in changes['orders']:
                                if order['order_type'] == 'limit' and f_limit_order.if_placed == True:
                                    if order['order_state'] == 'open' or order['order_id'] == f_limit_order.order.get('order_id', ''):
                                        f_limit_order.order = order
                                        f_limit_order.if_changing = False
                    elif changes['instrument_name'] == PERPETUAL:
                        if changes['trades']:
                            filled = sum([tx['amount'] if tx['order_type'] == 'limit' else 0 for tx in changes['trades']])
                            if filled > 0:
                                await self.deribittdreq.send_string(json.dumps({
                                    'accountid': DERIBIT_ACCOUNT_ID,
                                    'method': 'buy' if changes['trades'][0]['direction'] == 'sell' else 'sell',
                                    'params': {'instrument_name': SEASON_FUTURE, 'amount': filled, 'type': 'market',}
                                }))
                                await self.deribittdreq.recv_string()
                        if changes['positions']:
                            perpetual_size = changes['positions'][0]['size']
                        if changes['orders']:
                            for order in changes['orders']:
                                if order['order_type'] == 'limit' and p_limit_order.if_placed == True:
                                    if order['order_state'] == 'open' or order['order_id'] == p_limit_order.order.get('order_id', ''):
                                        p_limit_order.order = order
                                        p_limit_order.if_changing = False
                elif msg['type'] == 'user.portfolio':
                    portfolio = msg['data']
                    margin = [portfolio['equity'], portfolio['initial_margin'], portfolio['maintenance_margin']]
                elif msg['type'] == 'cancel_all':
                    f_limit_order.reset()
                    p_limit_order.reset()
                elif msg['type'] == 'positions':
                    for d in msg['data']:
                        if d['instrument_name'] == SEASON_FUTURE:
                            future_size = d['size']
                        elif d['instrument_name'] == PERPETUAL:
                            perpetual_size = d['size']
                elif msg['type'] in ('buy', 'sell', 'edit'):
                    pass
                    
                self.msg.task_done()
        except Exception as e:
            self.logger.exception(e)
            await self.process_msg()
            
    async def sub_msg_md(self):
        try:
            await asyncio.sleep(1)
            while self.state == ServiceState.started:
                task = asyncio.ensure_future(self.deribitmd.recv_string())
                done, pending = await asyncio.wait({task}, timeout=5)
                for t in pending:
                    t.cancel()
                msg = json.loads(done.pop().result()) if done else {}
                    
                if msg:
                    if msg['type'] == 'quote':
                        await self.msg.put(msg)
                else:
                    self.logger.info('cant receive msg from future md')
                    self.deribittdreq.send_string(json.dumps({
                        'accountid': DERIBIT_ACCOUNT_ID, 'method': 'cancel_all', 'params': {}
                    }))
                    self.deribittdreq.recv_string()
        except Exception as e:
            self.logger.exception(e)
            await self.sub_msg_md()

    async def sub_msg_td(self):
        try:
            await self.deribittdreq.send_string(json.dumps({
                'accountid': DERIBIT_ACCOUNT_ID, 'method': 'get_positions',
                'params': {'currency': SYMBOL, 'kind': 'future'}
            }))
            await self.deribittdreq.recv_string()
            while self.state == ServiceState.started:
                msg = json.loads(await self.deribittd.recv_string())
                if msg['accountid'] == DERIBIT_ACCOUNT_ID:
                    await self.msg.put(msg)
        except Exception as e:
            self.logger.exception(e)
            await self.sub_msg_td()

    async def run(self):
        if self.state == ServiceState.started:
            self.logger.error('tried to run service, but state is %s' % self.state)
        else:
            self.state = ServiceState.started
            asyncio.ensure_future(self.process_msg())
            asyncio.ensure_future(self.sub_msg_md())
            asyncio.ensure_future(self.sub_msg_td())

            
            
if __name__ == '__main__':
    service = FutureArbitrage('arb-new')
    start_service(service, {})