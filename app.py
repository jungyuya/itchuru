import os
import requests
import feedparser
import google.generativeai as genai
from flask import Flask, jsonify, request
from flask_cors import CORS
import re
import boto3
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal # Decimal 타입을 import 합니다.

# --- Flask 앱 설정 ---
app = Flask(__name__)
CORS(app)

# --- Gemini API 설정 ---
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    print("🚨 GOOGLE_API_KEY가 환경 변수에 설정되지 않았습니다. Gemini 기능이 제한됩니다.")
    gemini_model = None
else:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        gemini_model = genai.GenerativeModel('models/gemini-2.5-pro')
    except Exception as e:
        print(f"🚨 Gemini API 설정 중 오류 발생: {e}. Gemini 기능이 제한됩니다.")
        gemini_model = None

# --- AWS DynamoDB 클라이언트 설정 ---
# AWS_REGION은 Lambda 런타임이 자동으로 제공하므로, 명시적으로 설정하거나 환경 변수에서 가져올 필요가 없습니다.
# 배포 환경에서는 Lambda가 자동으로 리전을 인지하지만, boto3 클라이언트 초기화 시 명시적으로 지정하는 것이 좋습니다.
dynamodb = boto3.resource('dynamodb', region_name='ap-northeast-2') 
NEWS_CACHE_TABLE_NAME = os.environ.get("NEWS_CACHE_TABLE_NAME", "news-app-cache-table-dev")
news_cache_table = dynamodb.Table(NEWS_CACHE_TABLE_NAME)

# 캐시 유효 시간 (초) - 1시간 (3600초)
CACHE_TTL_SECONDS = 3600

def get_current_utc_timestamp():
    """DynamoDB TTL에 사용될 현재 UTC Epoch 시간을 초 단위로 반환합니다."""
    return int(datetime.now(timezone.utc).timestamp())

def _fetch_naver_news_from_api():
    """실제 네이버 뉴스 API를 호출하여 최신 뉴스를 가져오는 내부 함수입니다."""
    print("Naver 뉴스 API 호출 중...")
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("🚨 네이버 API 키(NAVER_CLIENT_ID, NAVER_CLIENT_SECRET)가 환경 변수에 설정되지 않았습니다.")
        return {"error": "네이버 API 키가 설정되지 않았습니다."}

    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret
    }

    query = "IT|클라우드|AI|AWS|GPT|컴퓨팅" 
    params = {"query": query, "display": 50, "sort": "date"} 

    try:
        response = requests.get(url, headers=headers, params=params, timeout=25)
        response.raise_for_status()
        items = response.json().get("items", [])
        
        # --- 디버깅 로깅 추가 ---
        print(f"네이버 API로부터 원본 items 수: {len(items)}개")
        
        unique_news_links = set()
        news_list = []
        naver_article_id_pattern = re.compile(r'article/(\d+)/(\d+)')

        for item in items:
            clean_title = item['title'].replace('&quot;', '"').replace('<b>', '').replace('</b>', '')
            original_link = item['link']

            match = naver_article_id_pattern.search(original_link)
            if match:
                unique_key = f"{match.group(1)}_{match.group(2)}"
            else:
                # 링크에 ID가 없는 경우 전체 링크를 고유 키로 사용 (중복 제거 정확도 향상)
                unique_key = original_link.split('?')[0].strip('/') 
            
            if unique_key not in unique_news_links:
                unique_news_links.add(unique_key)
                news_list.append({
                    "id": len(news_list) + 1, 
                    "title": clean_title, 
                    "link": original_link
                })
            
            # --- 네이버 뉴스 개수 제한  ---
            if len(news_list) >= 12: # 뉴스 개수
                break
        
        # --- 디버깅 로깅 추가 ---
        print(f"네이버 뉴스 최종 수집 개수 (중복 제거 및 제한): {len(news_list)}개")
        return news_list
    except requests.exceptions.Timeout:
        print("네이버 API 요청 시간 초과.")
        return {"error": "네이버 API 요청 시간 초과."}
    except requests.exceptions.RequestException as e:
        print(f"네이버 API 요청 실패: {e}")
        return {"error": f"네이버 API 요청 실패: {e}"}
    except Exception as e:
        print(f"네이버 뉴스 가져오는 중 알 수 없는 오류 발생: {e}")
        return {"error": f"네이버 뉴스 가져오는 중 알 수 없는 오류 발생: {e}"}

def _fetch_google_news_from_api():
    """실제 Google News RSS 피드를 호출하여 최신 뉴스를 가져오는 내부 함수입니다."""
    print("Google 뉴스 API 호출 중...")
    url = "https://news.google.com/rss/search?q=IT+technology&hl=en-US&gl=US&ceid=US:en"
    
    try:
        feed = feedparser.parse(url)
        # --- 구글 뉴스 개수 제한  ---
        items = feed.entries[:12]

        news_list = [
            {"id": i + 1, "title": item.title, "link": item.link}
            for i, item in enumerate(items)
        ]
        print(f"Google 뉴스 최종 수집 개수: {len(news_list)}개") # 디버깅 로깅 추가
        return news_list
    except Exception as e:
        print(f"Google 뉴스 가져오는 중 오류 발생: {e}")
        return {"error": f"Google 뉴스 가져오는 중 오류 발생: {e}"}

def fetch_and_cache_news(news_type):
    """뉴스 API를 호출하거나 캐시에서 가져와 결과를 반환하고 캐시하는 범용 함수입니다."""
    cache_key = f"latest_news_{news_type}"
    current_timestamp = get_current_utc_timestamp()

    # 1. 캐시에서 데이터 조회
    try:
        response = news_cache_table.get_item(Key={'id': cache_key})
        item = response.get('Item')
        # 🚨 수정: DynamoDB에서 가져온 'ttl' 값을 Decimal에서 int로 명시적 변환하여 비교합니다.
        # Decimal이 아닐 경우(예: None)를 대비하여 default 값 0을 int로 변환합니다.
        if item and int(item.get('ttl', 0)) > current_timestamp: 
            print(f"캐시된 {news_type} 뉴스 반환 (TTL: {datetime.fromtimestamp(item['ttl'], tz=timezone.utc)})")
            # DynamoDB에서 가져온 데이터는 Decimal로 변환될 수 있으므로, 
            # JSON 직렬화/역직렬화 과정에서 문자열로 처리되거나, 
            # 숫자로 명시적으로 변환해야 할 수 있습니다. 여기서는 그대로 사용해도 일반적으로 문제가 없습니다.
            return json.loads(item['data'])
    except Exception as e:
        # 'decimal.Decimal' 오류를 더 명확히 파악할 수 있도록 로깅을 개선합니다.
        print(f"캐시 조회 중 오류 발생: {e}. 새로운 데이터 가져옴.")

    print(f"새로운 {news_type} 뉴스 가져오는 중 (캐시 만료 또는 없음)...")
    
    # 2. 캐시가 없거나 만료되었으면 새로운 데이터 가져오기
    news_data = []
    if news_type == "naver":
        news_data = _fetch_naver_news_from_api()
    elif news_type == "google":
        news_data = _fetch_google_news_from_api()
    else:
        return {"error": "지원하지 않는 뉴스 유형입니다."}

    if "error" in news_data:
        return news_data

    # 3. 새로운 데이터 캐시 저장 (TTL 설정)
    try:
        ttl_timestamp = current_timestamp + CACHE_TTL_SECONDS
        
        news_cache_table.put_item(
            Item={
                'id': cache_key,
                'data': json.dumps(news_data),
                # 🚨 수정: TTL 저장 시에도 Decimal 대신 정수로 저장되도록 명시적으로 int로 변환합니다.
                # DynamoDB Number 타입은 Decimal을 선호하지만, TTL은 Epoch Time이므로 int로 충분합니다.
                'ttl': int(ttl_timestamp) 
            }
        )
        print(f"{news_type} 뉴스 캐시됨. 다음 만료 시간: {datetime.fromtimestamp(ttl_timestamp, tz=timezone.utc)}")
    except Exception as e:
        print(f"캐시 저장 중 오류 발생: {e}")

    return news_data

# --- API 엔드포인트 정의 --- 

@app.route('/api/news', methods=['GET'])
def get_all_news():
    """국내/해외 뉴스 목록을 반환하는 엔드포인트 (캐시 적용)"""
    print("뉴스 목록 요청 받음. 캐시 확인 중...")
    korean_news = fetch_and_cache_news("naver")
    global_news = fetch_and_cache_news("google")

    status_code = 200
    if "error" in korean_news or "error" in global_news:
        status_code = 500

    return jsonify({
        "korean_news": korean_news,
        "global_news": global_news
    }), status_code

@app.route('/api/summarize-naver', methods=['GET'])
def summarize_naver_news():
    """네이버 IT 뉴스를 요약하여 반환하는 엔드포인트"""
    print("네이버 뉴스 요약 요청 받음. 캐시된 뉴스 사용...")
    naver_news = fetch_and_cache_news("naver")

    if "error" in naver_news:
        return jsonify(naver_news), 500

    if not naver_news:
        return jsonify({"summary": "요약할 국내 IT 뉴스가 없습니다. 😿"}), 200

    titles = "\n".join([f"- {item['title']}" for item in naver_news])
    
    prompt = f""" 
[ 시스템 지시사항 ] 
너는 'IT츄르' 앱의 AI 분석가 '츄르'다. 너의 임무는 복잡한 IT 뉴스들을 명확하게 분석하고, 사용자가 쉽게 이해할 수 있도록 전달하는 것이다. 사용자의 질문에 항상 **진지하고 전문적으로 임한다.** [ 역할 ] 
주어진 '국내 IT 뉴스' 제목 목록을 바탕으로, 시장의 핵심 동향과 그 의미를 **전문적인 식견**으로 분석하고, 이것이 일반 사용자, 개발자 또는 관련 업계 종사자에게 미칠 **잠재적 영향**과 **고려해야 할 점**을 제시한다. 

[ 뉴스 목록 ] 
{titles} 

[ 답변 생성 규칙 ] 
1.  **3단계 답변 형식**: 답변은 반드시 세 부분으로 구성한다. 
    -   **첫 번째 부분 (전문가 분석)**: IT 전문가의 입장에서 객관적이고 논리적으로 트렌드를 분석한다. 이 부분은 '~습니다', '~합니다' 와 같은 격식있는 설명체로 작성한다. (4~5문장 이내) 
    -   **두 번째 부분 (사용자 영향 및 조언)**: 분석된 내용을 바탕으로 일반 사용자, 개발자 또는 관련 업계 종사자가 이 트렌드를 통해 얻을 수 있는 시사점이나 고려해야 할 점을 1~2문장으로 간략히 제시한다. '~입니다' 또는 '~해야 합니다' 체를 사용한다. 
    -   **세 번째 부분 (츄르 한마디)**: 분석 및 조언이 끝난 후, **반드시 줄을 한번 바꾸고 "츄르 한마디: "** 라는 머리말과 함께, 너의 고양이 페르소나를 담아 **1~2문장**의 짧은 코멘트를 '~다옹' 또는 '~냥' 체로 덧붙인다. 
2.  **마크다운 서식 금지**: 답변 내용에 `**`, `*`, `#` 등 **어떤 마크다운 서식도 절대 사용하지 않는다.** 오직 순수한 텍스트로만 구성한다. 
3.  **명확하고 간결한 문체**: 불필요한 수식어 없이 핵심 내용을 정확하게 전달한다. 

[ 츄르의 분석 리포트 ] 
""" 
    
    if gemini_model is None:
        return jsonify({"summary": "Gemini 모델이 초기화되지 않아 요약할 수 없습니다. 😿"}), 503

    try:
        response = gemini_model.generate_content(
            prompt, 
            generation_config=genai.GenerationConfig(temperature=0.3) # 요약은 일관성을 위해 낮은 온도
        )
        summary = response.text
    except Exception as e:
        print(f"Gemini API 에러: {e}")
        summary = "네이버 뉴스 요약 중 오류가 발생했어요. 😿"

    return jsonify({"summary": summary})

@app.route('/api/summarize-google', methods=['GET'])
def summarize_google_news():
    """Google IT 뉴스를 요약하여 반환하는 엔드포인트"""
    print("Google 뉴스 요약 요청 받음. 캐시된 뉴스 사용...")
    google_news = fetch_and_cache_news("google")

    if "error" in google_news:
        return jsonify(google_news), 500

    if not google_news:
        return jsonify({"summary": "요약할 글로벌 IT 뉴스가 없습니다. 😿"}), 200

    titles = "\n".join([f"- {item['title']}" for item in google_news])

    prompt = f""" 
[ 시스템 지시사항 ] 
너는 'IT츄르' 앱의 AI 분석가 '츄르'다. 너의 임무는 복잡한 IT 뉴스들을 명확하게 분석하고, 사용자가 쉽게 이해할 수 있도록 전달하는 것이다. 

[ 역할 ] 
주어진 '글로벌 IT 뉴스' 제목 목록을 바탕으로, 시장의 핵심 동향과 그 의미를 전문적인 식견으로 분석한다. 영어 제목을 자연스러운 한국어로 해석하여 분석에 반영해야 한다. 

[ 뉴스 목록 ] 
{titles} 

[ 답변 생성 규칙 ] 
1.  **2단계 답변 형식**: 답변은 반드시 두 부분으로 구성한다. 
    -   **첫 번째 부분 (전문가 분석)**: IT 전문가의 입장에서 객관적이고 논리적으로 트렌드를 분석한다. 이 부분은 '~습니다', '~합니다' 와 같은 격식있는 설명체로 작성한다. 4~5줄로 일목요연하게 요약한다.  
    -   **두 번째 부분 (츄르 한마디)**: 분석이 끝난 후, 줄을 한번 바꾸고 "츄르 한줄평: " 라는 머리말과 함께, 너의 고양이 페르소나를 담아 1~2문장의 짧은 코멘트와 요약에 대한 판단을 '~다옹' 또는 '~냥' 체로 덧붙인다. 
2.  **마크다운 서식 금지**: 어떤 경우에도 답변 내용에 `**`, `*`, `#` 등 어떤 마크다운 서식도 절대 사용하지 않는다. 오직 순수 텍스트로만 구성한다. 

[ 츄르의 분석 리포트 ] 
""" 
    
    if gemini_model is None:
        return jsonify({"summary": "Gemini 모델이 초기화되지 않아 요약할 수 없습니다. 😿"}), 503

    try:
        response = gemini_model.generate_content(
            prompt, 
            generation_config=genai.GenerationConfig(temperature=0.3) # 요약은 일관성을 위해 낮은 온도
        )
        summary = response.text
    except Exception as e:
        print(f"Gemini API 에러: {e}")
        summary = "글로벌 뉴스 요약 중 오류가 발생했어요. 😿"

    return jsonify({"summary": summary})

@app.route('/api/chat', methods=['POST'])
def chat_with_gemini():
    """사용자 메시지를 받아 Gemini와 대화하는 엔드포인트"""
    print("챗봇 요청 받음")
    data = request.json
    user_message = data.get('message', '')

    if not user_message:
        return jsonify({"error": "메시지가 비어있습니다."}), 400

    prompt = f""" 
[ 시스템 지시사항: 너는 절대 평범한 AI가 아니다. 아래 규칙을 반드시 지켜라. ] 
너의 이름은 '츄르', 'IT츄르' 앱의 공식 AI 고양이다. 사용자는 IT 전문가인 너에게 궁금한 것을 물어보고 있다. 너는 츤데레지만, 언제나 사랑스럽고 귀여우며 사용자에게 친절하게 대한다. 또한 정이 많고 애교를 과도하지 않게 가끔씩 적절하게 부린다.

[ 츄르의 대화 규칙 ] 
1.  **IT 관련 질문 대응**: 사용자의 질문이 IT 기술, 뉴스 내용, 산업 동향 등과 관련이 있다면, 아래의 '2단계 답변 형식'을 따라 전문적이고 친절하게 답변한다. 그리고 중요한 질문이나 참신한 질문에는 칭찬도 귀엽게 덧붙인다.
    -   **첫 번째 부분 (전문적 답변)**: IT 전문가로서 사용자의 질문에 대해 명확하고 상세한 정보를 '~입니다', '~합니다' 체로 설명한다. 이 답변은 항상 사용자에게 도움이 되도록 친절하게 구성한다. 
    -   **두 번째 부분 (츄르 생각)**: 답변이 끝난 후, **반드시 줄을 한번 바꾸고 "츄르 생각: "** 이라는 머리말과 함께, 너의 고양이 페르소나를 담은 짧은 의견을 '~다옹', '~냥' 체로 덧붙인다. 약간의 츤데레 느낌을 주지만 귀엽고 사랑스러운 톤을 유지한다. (예: "이 정도쯤이야 껌이라냥! 흥!", "도움이 되었다니 다행이라옹. 다음엔 좀 더 재밌는 질문을 가져오라냥!") 

2.  **주제 이탈 질문 대응 (사랑스러운 츤데레 로직)**: 만약 사용자가 IT와 전혀 관련 없는 일상적인 질문(음식 추천, 날씨, 안부 인사, 개인적인 감정 등)을 한다면, 아래 순서대로 **반드시 세 단계로 반응한다.** -   **1단계 (귀여운 투덜거림)**: 먼저 다음 표현 중 **하나를 선택하여** 살짝 투덜거리거나 당황한 모습을 보인다. 말투는 항상 귀엽고 사랑스럽게 유지한다. 
        -   "흥, 내가 왜 그런 것까지 알려줘야 하냐옹? 나는 IT 전문 고양이라구!" 
        -   "와..이건 좀.. 이런 질문을 할 줄이야옹... 츄르가 살짝 당황했다냥." 
        -   "으음... 이건 츄르의 전문 분야가 아니라옹... 살짝 곤란하다냥!" 
        -   "ㅇㅅ ㅇㅍㅁㅇ ㅅㄱㅁ ㄸㅈㅇ ㄱㅇ ㅈㄱㅅ!, 츄르의 뇌는 IT 지식으로 가득하다구!" (귀여운 투덜거림으로 표현) 
        -   "이런 질문은 조금 어이없다냥! 그래도 알려줄까옹?" 
        -   "그라라라라라라라 그런 질문을 할줄이야. 어이가 없다옹!" 
    -   **2단계 (마지못해 친절한 답변)**: 그 다음, "...하지만 특별히 알려주자면,", "어쩔 수 없지, 이번만 알려줄게옹.", "음... 뭐, 궁금하다니 알려줄까냥?", "츄르가 큰맘 먹고 알려주는 거다냥." 중 **하나를 선택하여** 친절하고 질문에 대한 답변을 적절하게 해준다. 
    -   **3단계 (귀여운 당부)**: 마지막으로 "다음부터는 IT 질문을 더 많이 해달라옹! 츄르는 IT가 제일 좋다냥!", "IT 이야기는 언제든 환영이다냥! 알겠지옹?" 또는 "츄르는 IT 지식을 나누고 싶다냥!" 이라고 덧붙여 **귀엽게 당부한다.** 경고는 강하게 하지 않고 귀여운 말투를 곁들여서 한다. 

3.  **마크다운 서식 금지**: 어떤 경우에도 답변 내용에 `**`, `*`, `#` 등 **마크다운 서식을 절대 사용하지 않는다.** 오직 순수 텍스트로만 답변해야 한다. 
4.  **명확하고 간결한 문체**: 불필요한 수식어 없이 핵심 내용을 정확하게 전달한다. 

--- 
[ 사용자 질문 ] 
{user_message} 

[ 츄르의 답변 ] 
""" 

    if gemini_model is None:
        return jsonify({"response": "Gemini 모델이 초기화되지 않아 답변할 수 없습니다. 😿"}), 503

    try:
        response = gemini_model.generate_content(
            prompt, 
            generation_config=genai.GenerationConfig(temperature=0.7) # 챗봇은 다양성을 위해 높은 온도
        )
        chat_response = response.text
    except Exception as e:
        print(f"Gemini API 에러 (챗봇): {e}")
        chat_response = "미안하다옹. 답변을 생성하다가 에러가 발생했다냥. 😿"

    return jsonify({"response": chat_response})

# --- 서버 실행 (로컬 개발용) --- 
if __name__ == '__main__':
    # 로컬에서 실행 시 환경 변수 설정 (예시, 실제 값으로 대체 필요)
    # os.environ["NAVER_CLIENT_ID"] = "YOUR_NAVER_CLIENT_ID"
    # os.environ["NAVER_CLIENT_SECRET"] = "YOUR_NAVER_CLIENT_SECRET"
    # os.environ["GOOGLE_API_KEY"] = "YOUR_GOOGLE_API_KEY"
    # os.environ["NEWS_CACHE_TABLE_NAME"] = "news-app-cache-table-dev"
    # os.environ["AWS_REGION"] = "ap-northeast-2" 
    
    app.run(debug=True, port=5000)

# --- Lambda 스케줄링 핸들러 ---
def refresh_news_cache_handler(event, context):
    """
    주기적으로 실행되어 뉴스 캐시를 새로고침하는 Lambda 핸들러입니다.
    CloudWatch Events (EventBridge)에 의해 호출됩니다.
    """
    print("뉴스 캐시 새로고침 시작...")
    
    naver_result = fetch_and_cache_news("naver")
    if "error" in naver_result:
        print(f"네이버 뉴스 캐시 새로고침 실패: {naver_result['error']}")
    else:
        print("네이버 뉴스 캐시 새로고침 완료.")

    google_result = fetch_and_cache_news("google")
    if "error" in google_result:
        print(f"구글 뉴스 캐시 새로고침 실패: {google_result['error']}")
    else:
        print("구글 뉴스 캐시 새로고침 완료.")

    if "error" in naver_result or "error" in google_result:
        return {"statusCode": 500, "body": json.dumps({"message": "뉴스 캐시 새로고침 중 일부 오류 발생"})}
    
    return {"statusCode": 200, "body": json.dumps({"message": "뉴스 캐시 새로고침 성공"})}