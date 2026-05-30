name: 뉴스봇 (10분마다)

on:
  schedule:
    - cron: '*/10 0-9 * * 1-5'
    - cron: '*/10 1-7 * * 6'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  news:
    runs-on: ubuntu-latest
    steps:
      - name: 체크아웃
        uses: actions/checkout@v4

      - name: Python 설정
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: 패키지 설치
        run: pip install requests feedparser yfinance

      - name: 뉴스봇 실행
        env:
          NEWS_BOT_TOKEN:    ${{ secrets.NEWS_BOT_TOKEN }}
          NEWS_CHAT_ID:      ${{ secrets.NEWS_CHAT_ID }}
          GITHUB_TOKEN:      ${{ secrets.GITHUB_TOKEN }}
          GITHUB_REPOSITORY: ${{ github.repository }}
        run: python news_bot.py
