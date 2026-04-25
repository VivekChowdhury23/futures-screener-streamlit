# Futures Screener (Streamlit)

A Streamlit dashboard that surfaces top NSE F&O stocks to trade based on intraday market sentiment, OI buildup patterns, and a multi-factor option-scoring engine.

## Features
- Live sentiment analysis across all F&O symbols (Long Build Up, Short Covering, etc.)
- Top-N shortlist of directional candidates
- 5-minute cumulative OI% and traded-contract charts (Plotly)
- Trade recommendation engine with delta, theta, IV, premium, and volume scoring

## Setup
```bash
pip install -r requirements.txt
streamlit run main.py
```

## Disclaimer
This is a screener for idea generation, not financial advice. Paper-trade signals before risking capital.