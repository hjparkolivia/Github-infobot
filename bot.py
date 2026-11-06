import os
import time
import base64
import requests
import asyncio
import google.generativeai as genai
from telegram import Bot

# 유저님이 등록한 이름(APIKEY_GEMINI) 그대로 가져오도록 설정!
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("APIKEY_GEMINI")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# 깃허브 401 에러 원천 차단 (인증 없이 공개 데이터만 검색)
github_headers = {
    "Accept": "application/vnd.github.v3+json"
}

async def main():
    telegram_bot = Bot(token=TELEGRAM_TOKEN)
    
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
        
        readme_url = f"https://api.github.com/repos/{repo_name}/readme"
        readme_response = requests.get(readme_url, headers=github_headers)
        
        readme_text = ""
        if readme_response.status_code == 200:
            readme_data = readme_response.json()
            readme_text = base64.b64decode(readme_data["content"]).decode("utf-8", errors="ignore")
        
        sliced_readme = readme_text[:1000]
        
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
            
            message = f"📈 **{repo_name}** (⭐ 별 {stars}개)\n🔗 {repo_url}\n\n💡 **AI 핵심 요약**\n{summary}"
            await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
            
        except Exception as e:
            print(f"에러 발생 ({repo_name}): {e}")
        
        time.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
