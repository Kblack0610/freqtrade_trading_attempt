"""The gene code GodStraNew and DevilStra share: the indicator a gene names, and the condition one operator draws from it.

Both are the upstream lookahead-biased controls (`user_data/strategies/lookahead_bias/`, family `biased-control`), so
`normalize` keeps its bias on purpose: it scales by the whole frame's min and max. The analysis keys a strategy's
cache on its own file's hash, so a change here reruns neither control: edit it only to keep them loadable.
"""

import freqtrade.vendor.qtpylib.indicators as qtpylib
import numpy as np
import talib.abstract as ta

# number of candles to check up,don,off trend.
TREND_CHECK_CANDLES = 4


def normalize(df):
    df = (df-df.min())/(df.max()-df.min())
    return df


def gene_calculator(dataframe, indicator):
    # Cuz Timeperiods not effect calculating CDL patterns recognations
    if 'CDL' in indicator:
        splited_indicator = indicator.split('-')
        splited_indicator[1] = "0"
        new_indicator = "-".join(splited_indicator)
        # print(indicator, new_indicator)
        indicator = new_indicator

    gene = indicator.split("-")

    gene_name = gene[0]
    gene_len = len(gene)

    if indicator in dataframe.keys():
        # print(f"{indicator}, calculated befoure")
        # print(len(dataframe.keys()))
        return dataframe[indicator]
    else:
        result = None
        # For Pattern Recognations
        if gene_len == 1:
            # print('gene_len == 1\t', indicator)
            result = getattr(ta, gene_name)(
                dataframe
            )
            return normalize(result)
        elif gene_len == 2:
            # print('gene_len == 2\t', indicator)
            gene_timeperiod = int(gene[1])
            result = getattr(ta, gene_name)(
                dataframe,
                timeperiod=gene_timeperiod,
            )
            return normalize(result)
        # For
        elif gene_len == 3:
            # print('gene_len == 3\t', indicator)
            gene_timeperiod = int(gene[2])
            gene_index = int(gene[1])
            result = getattr(ta, gene_name)(
                dataframe,
                timeperiod=gene_timeperiod,
            ).iloc[:, gene_index]
            return normalize(result)
        # For trend operators(MA-5-SMA-4)
        elif gene_len == 4:
            # print('gene_len == 4\t', indicator)
            gene_timeperiod = int(gene[1])
            sharp_indicator = f'{gene_name}-{gene_timeperiod}'
            dataframe[sharp_indicator] = getattr(ta, gene_name)(
                dataframe,
                timeperiod=gene_timeperiod,
            )
            return normalize(ta.SMA(dataframe[sharp_indicator].fillna(0), TREND_CHECK_CANDLES))
        # For trend operators(STOCH-0-4-SMA-4)
        elif gene_len == 5:
            # print('gene_len == 5\t', indicator)
            gene_timeperiod = int(gene[2])
            gene_index = int(gene[1])
            sharp_indicator = f'{gene_name}-{gene_index}-{gene_timeperiod}'
            dataframe[sharp_indicator] = getattr(ta, gene_name)(
                dataframe,
                timeperiod=gene_timeperiod,
            ).iloc[:, gene_index]
            return normalize(ta.SMA(dataframe[sharp_indicator].fillna(0), TREND_CHECK_CANDLES))


def condition_generator(dataframe, operator, indicator, crossed_indicator, real_num):

    condition = (dataframe['volume'] > 10)

    # TODO : it ill callculated in populate indicators.

    dataframe[indicator] = gene_calculator(dataframe, indicator)
    dataframe[crossed_indicator] = gene_calculator(
        dataframe, crossed_indicator)

    indicator_trend_sma = f"{indicator}-SMA-{TREND_CHECK_CANDLES}"
    if operator in ["UT", "DT", "OT", "CUT", "CDT", "COT"]:
        dataframe[indicator_trend_sma] = gene_calculator(
            dataframe, indicator_trend_sma)

    if operator == ">":
        condition = (
            dataframe[indicator] > dataframe[crossed_indicator]
        )
    elif operator == "=":
        condition = (
            np.isclose(dataframe[indicator], dataframe[crossed_indicator])
        )
    elif operator == "<":
        condition = (
            dataframe[indicator] < dataframe[crossed_indicator]
        )
    elif operator == "C":
        condition = (
            (qtpylib.crossed_below(dataframe[indicator], dataframe[crossed_indicator])) |
            (qtpylib.crossed_above(
                dataframe[indicator], dataframe[crossed_indicator]))
        )
    elif operator == "CA":
        condition = (
            qtpylib.crossed_above(
                dataframe[indicator], dataframe[crossed_indicator])
        )
    elif operator == "CB":
        condition = (
            qtpylib.crossed_below(
                dataframe[indicator], dataframe[crossed_indicator])
        )
    elif operator == ">R":
        condition = (
            dataframe[indicator] > real_num
        )
    elif operator == "=R":
        condition = (
            np.isclose(dataframe[indicator], real_num)
        )
    elif operator == "<R":
        condition = (
            dataframe[indicator] < real_num
        )
    elif operator == "/>R":
        condition = (
            dataframe[indicator].div(dataframe[crossed_indicator]) > real_num
        )
    elif operator == "/=R":
        condition = (
            np.isclose(dataframe[indicator].div(
                dataframe[crossed_indicator]), real_num)
        )
    elif operator == "/<R":
        condition = (
            dataframe[indicator].div(dataframe[crossed_indicator]) < real_num
        )
    elif operator == "UT":
        condition = (
            dataframe[indicator] > dataframe[indicator_trend_sma]
        )
    elif operator == "DT":
        condition = (
            dataframe[indicator] < dataframe[indicator_trend_sma]
        )
    elif operator == "OT":
        condition = (

            np.isclose(dataframe[indicator], dataframe[indicator_trend_sma])
        )
    elif operator == "CUT":
        condition = (
            (
                qtpylib.crossed_above(
                    dataframe[indicator],
                    dataframe[indicator_trend_sma]
                )
            ) &
            (
                dataframe[indicator] > dataframe[indicator_trend_sma]
            )
        )
    elif operator == "CDT":
        condition = (
            (
                qtpylib.crossed_below(
                    dataframe[indicator],
                    dataframe[indicator_trend_sma]
                )
            ) &
            (
                dataframe[indicator] < dataframe[indicator_trend_sma]
            )
        )
    elif operator == "COT":
        condition = (
            (
                (
                    qtpylib.crossed_below(
                        dataframe[indicator],
                        dataframe[indicator_trend_sma]
                    )
                ) |
                (
                    qtpylib.crossed_above(
                        dataframe[indicator],
                        dataframe[indicator_trend_sma]
                    )
                )
            ) &
            (
                np.isclose(
                    dataframe[indicator],
                    dataframe[indicator_trend_sma]
                )
            )
        )

    return condition, dataframe
