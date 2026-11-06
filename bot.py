import os
import time
import base64
import requests
import asyncio
import google.generativeai as genai
from telegram import Bot

# 1. 환경 변수 세팅 (GitHub Actions Secrets에서 가져옴)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") # 메시지를 받을 채팅방 ID
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Gemini 설정
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# 깃허브 API 요청 헤더
github_headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

async def main():
    telegram_bot = Bot(token=TELEGRAM_TOKEN)
    
    # 2. GitHub Search API 호출 (키워드 최적화)
    # 한글 검색보다 영어 검색이 훨씬 방대한 자료를 찾을 수 있습니다.
    query = "marketing automation OR video editing automation OR growth hacking stars:>20"
    search_url = f"https://api.github.com/search/repositories?q={query}&sort=updated&order=desc&per_page=3"
    
    print("GitHub 레포지토리 검색 중...")
    response = requests.get(search_url, headers=github_headers)
    
    if response.status_code != 200:
        print(f"GitHub API 에러: {response.status_code}")
        return

    repos = response.json().get("items", [])
    
    for repo in repos:
        repo_name = repo["full_name"]
        repo_url = repo["html_url"]
        stars = repo["stargazers_count"]
        description = repo["description"] or "설명 없음"
        
        # 3. 각 레포지토리의 README.md 가져오기
        readme_url = f"https://api.github.com/repos/{repo_name}/readme"
        readme_response = requests.get(readme_url, headers=github_headers)
        
        readme_text = ""
        if readme_response.status_code == 200:
            readme_data = readme_response.json()
            # base64로 인코딩된 텍스트 디코딩
            readme_text = base64.b64decode(readme_data["content"]).decode("utf-8", errors="ignore")
        
        # 토큰 절약을 위해 README 텍스트 앞 1000자만 슬라이싱
        sliced_readme = readme_text[:1000]
        
        # 4. Gemini API로 유용성 판단 및 요약
        prompt = f"""
        너는 마케팅 자동화, 영상 편집 자동화, 앱 사용자 확보를 위한 툴을 큐레이션하는 전문가야.
        다음 GitHub 레포지토리의 설명과 README 일부를 읽고, 이 툴이 마케팅이나 트래픽 확보, 영상 자동화에 실제로 유용한지 판단해 줘.
        유용하다면 어떤 핵심 기능이 있고 어떻게 쓸 수 있는지 한국어로 딱 3줄로 요약해 줘.
        
        레포지토리 설명: {description}
        README 일부: {sliced_readme}
        """
        
        try:
            print(f"[{repo_name}] Gemini 요약 중...")
            gemini_response = model.generate_content(prompt)
            summary = gemini_response.text.strip()
            
            # 5. 텔레그램으로 전송
            message = f"📈 **{repo_name}** (⭐ 별 {stars}개)\n🔗 {repo_url}\n\n💡 **AI 핵심 요약**\n{summary}"
            
            await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
            
        except Exception as e:
            print(f"에러 발생 ({repo_name}): {e}")
        
        # API Rate Limit 방지를 위해 2초 대기
        time.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())