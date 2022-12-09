'''
@Description: Basis-Trading bot
@Author: Yang Boyu
@Email: bradleyboyuyang@gmail.com
'''

import ccxt
import time
import traceback

from utils.Notifier import send_dd_msg
from utils.Logger import LOGGER


class BinanceArbBot:
    def __init__(self, exchange: ccxt.binance, coin: str, future_date: str, coin_precision: float, 
                 slippage: float, spot_fee_rate: float, contract_fee_rate: float, multiplier: dict, 
                 amount: float, num_maximum: int, threshold: float, max_trial: int, logger=LOGGER):
                 
        self.exchange = exchange
        self.coin = coin
        self.future_date = future_date
        self.coin_precision = coin_precision
        self.slippage = slippage
        self.spot_fee_rate = spot_fee_rate
        self.contract_fee_rate = contract_fee_rate
        self.multipler = multiplier
        self.amount = amount
        self.num_maximum = num_maximum
        self.threshold = threshold
        self.max_trial = max_trial
        self.logger = logger

        # symbols to be used as parameters
        self.spot_symbol = {'type1': coin + 'USDT', 'type2': coin + '/USDT'}
        self.future_symbol = {'type1': coin + 'USD_' + future_date}


    def retry_wrapper(self, func, params=dict(), act_name='', sleep_seconds=1, is_exit=True):
        """retry for stable connections with exchanges"""

        for _ in range(self.max_trial):
            try:
                # NOTE: reset the local timestamp when requesting again，otherwise requests may be declined
                if isinstance(params, dict) and 'timestamp' in list(params.keys()):
                    params['timestamp'] = int(time.time()) * 1000
                result = func(**params)
                return result
            except Exception as e:
                print('Function parameters:', params)
                self.logger.warning(f"{act_name} ({self.exchange.id}) FAIL | Retry after {sleep_seconds} seconds...")
                self.logger.warning(traceback.format_exc())
                time.sleep(sleep_seconds)
        else:
            self.logger.critical(f"{act_name} FAIL too many times... Arbitrage STOPs!")
            send_dd_msg(f"[Basis-Trading-Robot]\n\n {act_name} FAIL too many times... Arbitrage STOPs!\n\n" + 
            'Function parameters: \n' + str(params))
            if is_exit: exit()

    def binance_spot_place_order(self, symbol: str, direction: str, price: float, amount: float):
        """
        :description: placing limit orders in spot account
        :param symbol: spot symbol, eg. `BTC/USDT`
        :param direction:  `long` or `short`
        :param price:  spot open price
        :param amount: spot open amount
        """

        if direction == 'long':
            order_info = self.exchange.create_limit_buy_order(symbol, amount, price)
        elif direction == 'short':
            order_info = self.exchange.create_limit_sell_order(symbol, amount, price)
        else:
            raise ValueError('Parameter `direction` supports `long` or `short` only')
        self.logger.debug(f'spot orders ({self.exchange.id}) SUCCESS: {direction} > {symbol} > {amount} > {price}')
        self.logger.debug(f'Order info: {str(order_info)}')

        return order_info


    def binance_future_place_order(self, symbol: str, direction: str, price: float, amount: int):
        """
        :description: placing limit orders in coin-margin account
        :param symbol: coin-margin symbol, eg. `BTCUSD_210625`
        :param direction:  `open_long` or `open_short` or `close_long` or `close_short`
        :param price: coin-margin open price
        :param amount: coin-margin open amount (number of contracts, integer)

        TimeInForce parameter: 
        
        GTC - Good Till Cancel 成交为止
        IOC - Immediate or Cancel 无法立即成交(吃单)的部分就撤销
        FOK - Fill or Kill 无法全部立即成交就撤销
        GTX - Good Till Crossing 无法成为挂单方就撤销

        """

        if direction == 'open_short':
            side = 'SELL'
        elif direction == 'close_short':
            side = 'BUY'
        else:
            raise NotImplemented('Parameter `direction` only supports `open_short` and `close_short` currently')

        params = {
            'side': side,
            'positionSide': 'SHORT',
            'symbol': symbol,
            'type': 'LIMIT',
            'price': price, 
            'quantity': amount,  # number of contracts
            'timeInForce': 'GTC',  # check API
        }
        params['timestamp'] = int(time.time() * 1000)
        order_info = self.exchange.dapiPrivatePostOrder(params)
        self.logger.debug(f'({self.exchange.id}) Future orders SUCCESS: {direction} > {symbol} > {amount} > {price}')
        self.logger.debug(f'Order info: {str(order_info)}')

        return order_info


    def binance_account_transfer(self, currency: str, amount, from_account='spot', to_account='coin-margin'):
        """
        User Universal Transfer between spot account and coin-margin account
        POST /sapi/v1/asset/transfer (HMAC SHA256)
        """

        if from_account == 'spot' and to_account == 'coin-margin':
            transfer_type = 'MAIN_CMFUTURE'
        elif from_account == 'coin-margin' and to_account == 'spot':
            transfer_type = 'CMFUTURE_MAIN'
        else:
            raise ValueError('Cannot recognize parameters for User Universal Transfer')

        params = {
            'type': transfer_type,
            'asset': currency,
            'amount': amount,
        }
        params['timestamp'] = int(time.time() * 1000)
        transfer_info = self.exchange.sapiPostAssetTransfer(params=params)
        self.logger.debug(f"({self.exchange.id}) Transfer SUCCESS: {from_account} --> {to_account} > amount: {amount}")
        self.logger.debug(f'Transfer info: {str(transfer_info)}')

        return transfer_info

    def open_position(self):
        """open positions for basis trading"""
        execute_num = 0

        while True:
            # get the best ask for spot
            spot_ask1 = self.exchange.publicGetTickerBookTicker(params={'symbol': self.spot_symbol['type1']})['askPrice']

            # get the best bid for coin-margin
            coin_bid1 = self.exchange.dapiPublicGetTickerBookTicker(params={'symbol': self.future_symbol['type1']})[0]['bidPrice']

            # detect price difference
            r = float(coin_bid1) / float(spot_ask1) - 1
            operator = '>' if spot_ask1 > coin_bid1 else '<'
            self.logger.info('Spot %.4f %s COIN-M %.4f -> Price Difference: %.4f%%' % (float(spot_ask1), operator, float(coin_bid1), r * 100))

            # whether to start arbitrage (must be contango)
            if r < self.threshold:
                self.logger.info('Price difference SMALLER than threshold >>> Retrying...')
            else:
                self.logger.debug('Price difference LARGER than threshold >>> Starting arbitrage...')

                # long spot, short coin-margin
                contract_num = int(self.amount / self.multipler[self.coin])  
                contract_coin_num = contract_num * self.multipler[self.coin] / float(coin_bid1)  
                contract_fee = contract_coin_num * self.contract_fee_rate  
                spot_amount = contract_coin_num / (1 - self.spot_fee_rate) + contract_fee  
                self.logger.debug(f'Arbitrage starts >>> future cotract num {contract_num} > coin-margin num {contract_coin_num} > fee {contract_fee} > spot amount {spot_amount}')

                # TODO: limit long order (spot) with slippage
                price = float(spot_ask1) * (1 + self.slippage)
                params = {
                    'symbol': self.spot_symbol['type2'],
                    'direction': 'long',
                    'price': price,  
                    'amount': spot_amount,
                }
                spot_order_info = self.retry_wrapper(func=self.binance_spot_place_order, params=params, act_name='Long spot orders')

                # TODO: limit short order (coin-margin) with slippage
                price = float(coin_bid1) * (1 - self.slippage)
                price = round(price, self.coin_precision)
                params = {
                    'symbol': self.future_symbol['type1'],
                    'direction': 'open_short',
                    'price': price, 
                    'amount': contract_num,
                }
                future_order_info = self.retry_wrapper(func=self.binance_future_place_order, params=params, act_name='Short coin-margin orders')

                # NOTE: it is possible that the spot account has not yet updated 
                time.sleep(2) 

                balance = self.exchange.fetch_balance()
                num = balance[self.coin]['free']
                self.logger.debug(f'Amount to be transfered > {num}')

                # transfer from spot account to coin-margin account
                params = {
                    'currency': self.coin,
                    'amount': num,
                    'from_account': 'spot', 
                    'to_account': 'coin-margin',
                }
                self.retry_wrapper(func=self.binance_account_transfer, params=params, act_name='Transfer (SPOT --> COIN-M)')

                execute_num += 1

                self.logger.info(f"Number of opening executions: {execute_num}")

                print(spot_order_info['average'])
                print(future_order_info)

            # logger.info('*'* 25 + "Current loop finishes" + '*'*25)

            time.sleep(2)

            if execute_num >= self.num_maximum:
                self.logger.info('Maximum execution number reached >>> Position opening stops.')
                break

    def close_position_utils(self):
        """close positions for basis trading"""
        # get USDT balance in spot account
        balance = self.exchange.fetch_balance()
        num = balance['USDT']['free']
        self.logger.info(f'Amount of USDT in spot account：{num}')

        # get coin-margin balance spot account
        balance = self.exchange.fetch_balance()
        num = balance[self.coin]['free']
        self.logger.info(f'Amount of {self.coin} in coin-margin account：{num}')

        if num < self.amount:
            self.logger.error('Please ensure the coin-margin remaining balance is enough!')
            # exit()

        now_execute_num = 0

        while True:
            # get the best ask for spot
            spot_ask1 = self.exchange.publicGetTickerBookTicker(params={'symbol': self.spot_symbol['type1']})['bidPrice']
            spot_ask1 = float(spot_ask1)
            # get the best bid for coin-margin
            coin_bid1 = self.exchange.dapiPublicGetTickerBookTicker(params={'symbol': self.future_symbol['type1']})[0]['askPrice']
            coin_bid1 = float(coin_bid1)

            # detect price difference
            r = coin_bid1 / spot_ask1 - 1
            operator = '>' if spot_ask1 > coin_bid1 else '<'
            self.logger.info('Spot %.4f %s COIN-M %.4f -> Price Difference: %.4f%%' % (float(spot_ask1), operator, float(coin_bid1), r * 100))

            # whether to stop arbitrage
            if r > self.threshold:
                self.logger.info('Price difference LARGER than threshold >>> Retrying...')
            else:
                self.logger.debug('Price difference SMALLER than threshold >>> Stopping arbitrage...')

                # close long for spot, close short for coin-margin
                contract_num = int(coin_bid1 * self.amount / self.multipler[self.coin]) 
                contract_coin_num = contract_num * self.multipler[self.coin] / coin_bid1 
                contract_fee = contract_coin_num * self.contract_fee_rate 
                spot_amount = contract_coin_num - contract_fee  # spot selling amount = contract coin num - contract fee
                # spot_amount = round(spot_amount, 3)
                self.logger.debug(f'Closing contract num {contract_num} > equivalent coin num {contract_coin_num} > contract fee {contract_fee} > spot selling amount {spot_amount}')

                # TODO: limit long order (coin-margin) with slippage
                price = coin_bid1 * (1 + self.slippage)
                price = round(price, self.coin_precision)

                params = {
                    'symbol': self.future_symbol['type1'],
                    'direction': 'close_short',
                    'price': price, 
                    'amount': contract_num,
                }
                future_order_info = self.retry_wrapper(func=self.binance_future_place_order, params=params, act_name='Close short coin-margin orders')

                # TODO: limit short order (spot) with slippage
                price = spot_ask1 * (1 - self.slippage)
                params = {
                    'symbol': self.spot_symbol['type2'],
                    'direction': 'short',
                    'price': price,  
                    'amount': spot_amount,
                }
                spot_order_info = self.retry_wrapper(func=self.binance_spot_place_order, params=params, act_name='Long spot orders')

                # NOTE: it is possible that the coin-margin account has not yet updated 
                time.sleep(2)

                # transfer from coin-margin to spot account (for preparation)
                params = {
                    'currency': self.coin,
                    # 'amount': self.amount,
                    'amount': num,
                    'from_account': 'coin-margin', 
                    'to_account': 'spot',
                }
                self.retry_wrapper(func=self.binance_account_transfer, params=params, act_name='Transfer (COIN-M --> SPOT)')

                now_execute_num = now_execute_num + 1

                print(spot_order_info['average'])
                print(future_order_info)

            self.logger.info(f"Number of closing executions: {now_execute_num}")

            # logger.info('*'* 25 + "Current loop finishes" + '*'*25)
            time.sleep(2)

            if now_execute_num >= self.num_maximum:
                self.logger.info('Maximum execution number reached >>> Position closing stops.')
                
                exit()

    def close_position(self):
            while True:
                try:
                    self.close_position_utils()
                except Exception as e:
                    self.logger.critical(f'Closing positions FAILED >>> Retrying...')
                    self.logger.warning(traceback.format_exc())
                    time.sleep(2)