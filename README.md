# Google Places Flask Demo

Google Places API를 사용해 장소를 검색하는 Flask 예제입니다.

## 기능
- 텍스트 검색: 예) 강남 카페, Tokyo ramen
- 위치 중심 검색: 위도/경도와 반경 기준 검색
- 장소 이름, 주소, 평점, 영업 상태, Google Maps 링크 표시

## 준비
1. Google Cloud Console에서 Places API를 활성화합니다.
2. API 키를 발급합니다.
3. `.env.example`를 복사해 `.env`를 만들고 값을 채웁니다.

## 실행
```powershell
cd GooglePlacesFlask
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

브라우저에서 `http://127.0.0.1:5000` 으로 접속합니다.

## 환경변수
- `GOOGLE_PLACES_API_KEY`: Google Places API 키
- `FLASK_SECRET_KEY`: Flask 세션 키
- `FLASK_DEBUG`: `true` 또는 `false`

## 참고
- 서버가 Google Places API를 호출하므로 API 키를 프런트엔드에 직접 노출하지 않습니다.
- 기본 구현은 Places API v1 `places:searchText` 엔드포인트를 사용합니다.
