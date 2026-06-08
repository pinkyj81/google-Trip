from pathlib import Path
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, unquote, urlparse

import pyodbc
import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'google-places-demo-secret')
DB_SERVER = os.getenv('DB_SERVER', 'ms1901.gabiadb.com')
DB_DATABASE = os.getenv('DB_DATABASE', 'yujincast')
DB_USERNAME = os.getenv('DB_USERNAME', '')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_DRIVER = os.getenv('DB_DRIVER', 'ODBC Driver 18 for SQL Server')
PLACES_TEXT_SEARCH_URL = 'https://places.googleapis.com/v1/places:searchText'
FIELD_MASK = ','.join([
    'nextPageToken',
    'places.name',
    'places.displayName',
    'places.formattedAddress',
    'places.location',
    'places.primaryType',
    'places.rating',
    'places.userRatingCount',
    'places.priceLevel',
    'places.businessStatus',
    'places.nationalPhoneNumber',
    'places.googleMapsUri',
    'places.websiteUri',
    'places.types',
    'places.photos.name',
])

DB_NOTE_MAX_LEN = 1000


def _installed_sql_server_drivers() -> list[str]:
    return [driver for driver in pyodbc.drivers() if 'SQL Server' in driver]


def _resolve_db_driver() -> str:
    installed = _installed_sql_server_drivers()
    if DB_DRIVER and DB_DRIVER in installed:
        return DB_DRIVER

    for preferred in ('ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server', 'SQL Server'):
        if preferred in installed:
            return preferred

    if DB_DRIVER:
        return DB_DRIVER

    raise RuntimeError('사용 가능한 SQL Server ODBC 드라이버가 없습니다.')


def _get_mssql_conn() -> pyodbc.Connection:
    if not DB_USERNAME or not DB_PASSWORD:
        raise RuntimeError('DB_USERNAME, DB_PASSWORD를 .env에 설정하세요.')

    driver_name = _resolve_db_driver()

    conn_str = (
        f'DRIVER={{{driver_name}}};'
        f'SERVER={DB_SERVER};'
        f'DATABASE={DB_DATABASE};'
        f'UID={DB_USERNAME};'
        f'PWD={DB_PASSWORD};'
        'Encrypt=yes;'
        'TrustServerCertificate=yes;'
    )
    conn = pyodbc.connect(conn_str)
    return conn


def _fit_db_text(value: str, max_len: int = DB_NOTE_MAX_LEN) -> str:
    text = str(value or '').strip()
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _ensure_location_info_columns(conn: pyodbc.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.LocationInfo', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.LocationInfo', 'GoogleMapsUrl') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.LocationInfo ADD GoogleMapsUrl NVARCHAR(1000) NULL')
        END
        '''
    )
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.LocationInfo', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.LocationInfo', 'Address') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.LocationInfo ADD [Address] NVARCHAR(500) NULL')
        END
        '''
    )
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.LocationInfo', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.LocationInfo', 'Latitude') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.LocationInfo ADD Latitude FLOAT NULL')
        END
        '''
    )
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.LocationInfo', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.LocationInfo', 'Longitude') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.LocationInfo ADD Longitude FLOAT NULL')
        END
        '''
    )
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.LocationInfo', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.LocationInfo', 'Category') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.LocationInfo ADD Category NVARCHAR(100) NULL')
        END
        '''
    )
    conn.commit()


def _ensure_location_option_table(conn: pyodbc.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.location_info_options', 'U') IS NULL
        BEGIN
            CREATE TABLE dbo.location_info_options (
                id INT IDENTITY(1,1) PRIMARY KEY,
                Country NVARCHAR(100) NOT NULL,
                City NVARCHAR(100) NOT NULL,
                created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
            )
        END
        '''
    )
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.location_info_options', 'U') IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
               FROM sys.indexes
               WHERE name = 'UX_location_info_options_country_city'
                 AND object_id = OBJECT_ID('dbo.location_info_options')
           )
        BEGIN
            EXEC('CREATE UNIQUE INDEX UX_location_info_options_country_city ON dbo.location_info_options(Country, City)')
        END
        '''
    )
    conn.commit()


def _extract_user_memo_text(note_text: str) -> str:
    text = str(note_text or '').strip()
    if not text:
        return ''

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    memo_line = next((line for line in lines if line.startswith('[MEMO]')), '')
    if memo_line:
        return memo_line.replace('[MEMO]', '', 1).strip()

    filtered = [line for line in lines if not line.startswith('[PHOTO]') and not line.startswith('[LINK]')]
    return '\n'.join(filtered).strip() if filtered else text


def _ensure_planner_tables(conn: pyodbc.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trips', 'U') IS NULL
        BEGIN
            CREATE TABLE dbo.travel_trips (
                id INT IDENTITY(1,1) PRIMARY KEY,
                title NVARCHAR(200) NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                flight_info NVARCHAR(500) NULL,
                created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
            )
        END
        '''
    )
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trip_places', 'U') IS NULL
        BEGIN
            CREATE TABLE dbo.travel_trip_places (
                id INT IDENTITY(1,1) PRIMARY KEY,
                trip_id INT NOT NULL,
                place_name NVARCHAR(200) NOT NULL,
                place_category NVARCHAR(50) NULL,
                formatted_address NVARCHAR(500) NULL,
                google_maps_uri NVARCHAR(1000) NULL,
                latitude FLOAT NULL,
                longitude FLOAT NULL,
                memo NVARCHAR(1000) NULL,
                created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
                CONSTRAINT FK_travel_trip_places_trips FOREIGN KEY (trip_id)
                    REFERENCES dbo.travel_trips(id) ON DELETE CASCADE
            )
        END
        '''
    )
    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trip_schedule', 'U') IS NULL
        BEGIN
            CREATE TABLE dbo.travel_trip_schedule (
                id INT IDENTITY(1,1) PRIMARY KEY,
                trip_id INT NOT NULL,
                schedule_date DATE NOT NULL,
                schedule_time VARCHAR(5) NOT NULL DEFAULT '10:00',
                trip_place_id INT NOT NULL,
                note NVARCHAR(1000) NULL,
                created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
                CONSTRAINT FK_travel_trip_schedule_trip FOREIGN KEY (trip_id)
                    REFERENCES dbo.travel_trips(id),
                CONSTRAINT FK_travel_trip_schedule_place FOREIGN KEY (trip_place_id)
                    REFERENCES dbo.travel_trip_places(id) ON DELETE CASCADE
            )
        END
        '''
    )

    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trips', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.travel_trips', 'flight_info') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.travel_trips ADD flight_info NVARCHAR(500) NULL')
        END
        '''
    )

    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trip_places', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.travel_trip_places', 'place_category') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.travel_trip_places ADD place_category NVARCHAR(50) NULL')
        END
        '''
    )

    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trip_places', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.travel_trip_places', 'latitude') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.travel_trip_places ADD latitude FLOAT NULL')
        END
        '''
    )

    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trip_places', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.travel_trip_places', 'longitude') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.travel_trip_places ADD longitude FLOAT NULL')
        END
        '''
    )

    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trip_schedule', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.travel_trip_schedule', 'schedule_time') IS NULL
        BEGIN
            EXEC('ALTER TABLE dbo.travel_trip_schedule ADD schedule_time VARCHAR(5) NULL')
        END
        '''
    )

    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trip_schedule', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.travel_trip_schedule', 'schedule_time') IS NOT NULL
        BEGIN
            EXEC('UPDATE dbo.travel_trip_schedule SET schedule_time = ''10:00'' WHERE schedule_time IS NULL')
        END
        '''
    )

    cursor.execute(
        '''
        IF OBJECT_ID('dbo.travel_trip_schedule', 'U') IS NOT NULL
           AND COL_LENGTH('dbo.travel_trip_schedule', 'schedule_time') IS NOT NULL
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM sys.indexes
                WHERE name = 'UX_travel_trip_schedule_trip_date_place'
                  AND object_id = OBJECT_ID('dbo.travel_trip_schedule')
            )
            BEGIN
                EXEC('DROP INDEX UX_travel_trip_schedule_trip_date_place ON dbo.travel_trip_schedule')
            END

            IF NOT EXISTS (
                SELECT 1
                FROM sys.indexes
                WHERE name = 'UX_travel_trip_schedule_trip_date_place'
                  AND object_id = OBJECT_ID('dbo.travel_trip_schedule')
            )
            BEGIN
                EXEC('CREATE UNIQUE INDEX UX_travel_trip_schedule_trip_date_place ON dbo.travel_trip_schedule(trip_id, schedule_date, schedule_time, trip_place_id)')
            END
        END
        '''
    )

    conn.commit()


def _rows_to_dicts(cursor, rows) -> list[dict]:
    if not cursor.description:
        return []
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in rows if row is not None]


def _row_to_dict(cursor, row) -> dict | None:
    if row is None or not cursor.description:
        return None
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def _has_schedule_time_column(conn: pyodbc.Connection) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT CASE WHEN COL_LENGTH('dbo.travel_trip_schedule', 'schedule_time') IS NULL THEN 0 ELSE 1 END"
    )
    row = cursor.fetchone()
    return bool(row and int(row[0]) == 1)


def _validate_ymd(date_text: str) -> str:
    datetime.strptime(date_text, '%Y-%m-%d')
    return date_text


def _validate_hhmm(time_text: str) -> str:
    parsed = datetime.strptime(time_text, '%H:%M')
    return parsed.strftime('%H:%M')


def _iter_trip_days(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, '%Y-%m-%d').date()
    end = datetime.strptime(end_date, '%Y-%m-%d').date()
    days: list[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.strftime('%Y-%m-%d'))
        cursor = cursor + timedelta(days=1)
    return days


@app.errorhandler(HTTPException)
def handle_http_exception(exc: HTTPException):
    if request.path.startswith('/api/'):
        return jsonify({'error': exc.description}), exc.code
    return exc


@app.errorhandler(Exception)
def handle_unexpected_exception(exc: Exception):
    if request.path.startswith('/api/'):
        return jsonify({'error': f'서버 내부 오류: {exc}'}), 500
    raise exc


def _get_places_api_key() -> str:
    """Read API key from runtime env/.env only (never from .env.example template)."""
    key = os.getenv('GOOGLE_PLACES_API_KEY', '').strip()
    if key and key != 'your_google_places_api_key':
        return key

    # Reload real env file for local development
    load_dotenv(BASE_DIR / '.env', override=True)
    key = os.getenv('GOOGLE_PLACES_API_KEY', '').strip()
    if key and key != 'your_google_places_api_key':
        return key
    return ''


def _parse_float(value: str | None) -> float | None:
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int | None:
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_payload(data: dict) -> dict:
    auto_recommend = bool(data.get('auto_recommend'))
    query = (data.get('query') or '').strip()
    if not query and not auto_recommend:
        raise ValueError('검색어를 입력하세요.')

    search_type = (data.get('search_type') or 'text').strip().lower()
    text_query = query or '관광 명소 추천'
    payload = {
        'textQuery': text_query,
        'pageSize': 10,
        'languageCode': 'ko',
    }

    place_category = (data.get('place_category') or '').strip().lower()
    allowed_categories = {
        'tourist_attraction',
        'historical_landmark',
        'restaurant',
        'cafe',
        'lodging',
        'museum',
        'park',
        'point_of_interest',
    }
    if place_category:
        if place_category not in allowed_categories:
            raise ValueError('지원하지 않는 카테고리입니다. 관광명소/랜드마크/레스토랑/카페 등에서 선택하세요.')
        payload['includedType'] = place_category

    country_code = (data.get('country_code') or '').strip().lower()
    if country_code:
        if len(country_code) != 2 or not country_code.isalpha():
            raise ValueError('국가 고정 코드는 2자리 영문이어야 합니다. 예: KR, FR, JP')
        payload['regionCode'] = country_code

    if auto_recommend:
        # 도시명만 입력해도 관광지 의도를 명확히 전달해 결과 수를 늘린다.
        payload['textQuery'] = f'{text_query} 관광 명소 추천'
        payload['pageSize'] = 20
        payload['rankPreference'] = 'RELEVANCE'

    latitude = _parse_float(data.get('latitude'))
    longitude = _parse_float(data.get('longitude'))
    radius = _parse_float(data.get('radius')) or 3000

    if search_type == 'nearby':
        if latitude is None or longitude is None:
            raise ValueError('위치 중심 검색은 위도와 경도가 필요합니다.')
        payload['locationBias'] = {
            'circle': {
                'center': {
                    'latitude': latitude,
                    'longitude': longitude,
                },
                'radius': max(100, min(radius, 50000)),
            }
        }
    # 텍스트 검색(text)에서는 위치값이 입력되어 있어도 무시한다.
    # 의도치 않게 이전 좌표(예: 김해)로 검색이 치우치는 문제를 방지한다.

    return payload


def _call_places_api(payload: dict) -> dict:
    places_api_key = _get_places_api_key()
    if not places_api_key or places_api_key == 'your_google_places_api_key':
        raise RuntimeError('GOOGLE_PLACES_API_KEY 값을 GooglePlacesFlask/.env 파일에 설정하세요.')

    response = requests.post(
        PLACES_TEXT_SEARCH_URL,
        headers={
            'Content-Type': 'application/json',
            'X-Goog-Api-Key': places_api_key,
            'X-Goog-FieldMask': FIELD_MASK,
        },
        json=payload,
        timeout=20,
    )

    if response.status_code >= 400:
        try:
            message = response.json().get('error', {}).get('message')
        except ValueError:
            message = response.text
        raise RuntimeError(f'Google Places API 오류: {message or response.status_code}')

    return response.json()


def _normalize_places(raw_places: list[dict]) -> list[dict]:
    """Flatten Places API v1 response for UI rendering."""
    normalized: list[dict] = []
    for place in raw_places:
        display_name = place.get('displayName', '')
        if isinstance(display_name, dict):
            display_name = display_name.get('text', '')

        photo_name = ''
        photos = place.get('photos') or []
        if photos and isinstance(photos, list) and isinstance(photos[0], dict):
            photo_name = photos[0].get('name', '') or ''

        normalized.append({
            'displayName': display_name,
            'formattedAddress': place.get('formattedAddress'),
            'latitude': (place.get('location') or {}).get('latitude'),
            'longitude': (place.get('location') or {}).get('longitude'),
            'rating': place.get('rating'),
            'userRatingCount': place.get('userRatingCount'),
            'businessStatus': place.get('businessStatus'),
            'googleMapsUri': place.get('googleMapsUri'),
            'websiteUri': place.get('websiteUri'),
            'types': place.get('types', []),
            'photoUrl': f"/api/place-photo?photo_name={quote(photo_name)}&max_height=360" if photo_name else '',
        })
    return normalized


def _flatten_place_for_table(place: dict) -> dict:
    display_name = place.get('displayName', '')
    if isinstance(display_name, dict):
        display_name = display_name.get('text', '')

    location = place.get('location') or {}
    photos = place.get('photos') or []

    return {
        'name': display_name,
        'resource_name': place.get('name', ''),
        'primary_type': place.get('primaryType', ''),
        'types': ', '.join(place.get('types', []) or []),
        'rating': place.get('rating'),
        'user_rating_count': place.get('userRatingCount'),
        'price_level': place.get('priceLevel', ''),
        'business_status': place.get('businessStatus', ''),
        'formatted_address': place.get('formattedAddress', ''),
        'latitude': location.get('latitude'),
        'longitude': location.get('longitude'),
        'phone': place.get('nationalPhoneNumber', ''),
        'website': place.get('websiteUri', ''),
        'google_maps': place.get('googleMapsUri', ''),
        'photo_count': len(photos),
    }


def _extract_coords_and_name_from_maps_url(url_text: str) -> tuple[float | None, float | None, str]:
    text = str(url_text or '').strip()
    if not text:
        return None, None, ''

    decoded_text = text
    for _ in range(4):
        next_value = unquote(decoded_text)
        if next_value == decoded_text:
            break
        decoded_text = next_value

    lat = None
    lng = None
    name = ''

    # Prefer place-detail coordinates (!3d...!4d...) over map viewport center (@lat,lng).
    # Some Google Maps URLs contain both, and @ points to current camera center, not the POI.
    d_matches = re.findall(r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)', decoded_text)
    if d_matches:
        last_lat, last_lng = d_matches[-1]
        lat = float(last_lat)
        lng = float(last_lng)

    if lat is None or lng is None:
        at_match = re.search(r'@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)', decoded_text)
        if at_match:
            lat = float(at_match.group(1))
            lng = float(at_match.group(2))

    # Some share links use /{lat},{lng},17z style without '@'.
    if lat is None or lng is None:
        path_match = re.search(r'/(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,|/)', decoded_text)
        if path_match:
            lat = float(path_match.group(1))
            lng = float(path_match.group(2))

    parsed = urlparse(decoded_text)
    qs = parse_qs(parsed.query)
    for key in ('q', 'query', 'll', 'destination', 'center', 'sll', 'daddr', 'origin'):
        if lat is not None and lng is not None:
            break
        raw = (qs.get(key) or [''])[0]
        m = re.search(r'(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)', raw)
        if m:
            lat = float(m.group(1))
            lng = float(m.group(2))

    # Last-resort scan over the whole URL text for a valid coordinate pair.
    if lat is None or lng is None:
        for m in re.finditer(r'(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)', decoded_text):
            cand_lat = float(m.group(1))
            cand_lng = float(m.group(2))
            if -90 <= cand_lat <= 90 and -180 <= cand_lng <= 180:
                lat = cand_lat
                lng = cand_lng
                break

    def _decode_percent_text(raw_text: str) -> str:
        value = str(raw_text or '').strip()
        if not value:
            return ''

        decoded = value
        for _ in range(4):
            next_value = unquote(decoded)
            if next_value == decoded:
                break
            decoded = next_value

        return decoded.replace('+', ' ').strip()

    path_place = re.search(r'/place/([^/]+)', parsed.path, re.IGNORECASE)
    if path_place and path_place.group(1):
        name = _decode_percent_text(path_place.group(1))

    if not name:
        q = (qs.get('q') or qs.get('query') or [''])[0].strip()
        if q and not re.search(r'^-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?$', q):
            name = _decode_percent_text(q)

    if lat is not None and (lat < -90 or lat > 90):
        lat = None
    if lng is not None and (lng < -180 or lng > 180):
        lng = None

    return lat, lng, name


def _extract_embedded_maps_url(html_text: str) -> str:
    text = str(html_text or '')
    if not text:
        return ''

    patterns = [
        r'https?://www\.google\.com/maps[^\"\'\s<>]+',
        r'https?://maps\.google\.com[^\"\'\s<>]+',
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(0).replace('\\u0026', '&').replace('\\/', '/')

    escaped_patterns = [
        r'https:\\/\\/www\\.google\\.com\\/maps[^\"\'\s<>]+',
        r'https:\\/\\/maps\\.google\\.com[^\"\'\s<>]+',
    ]
    for pattern in escaped_patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(0).replace('\\u0026', '&').replace('\\/', '/')

    return ''


def _is_static_map_url(url_text: str) -> bool:
    value = str(url_text or '').lower()
    return '/maps/api/staticmap' in value or 'staticmap' in value


def _extract_meta_image_url(html_text: str) -> str:
    text = str(html_text or '')
    if not text:
        return ''

    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip().replace('&amp;', '&')
    return ''


def _extract_googleusercontent_image_url(html_text: str) -> str:
    text = str(html_text or '')
    if not text:
        return ''

    normalized = text.replace('\\u0026', '&').replace('\\/', '/')
    patterns = [
        r'https?://lh\d+\.googleusercontent\.com/[^"\'\s<>]+',
        r'https?://lh\d+\.ggpht\.com/[^"\'\s<>]+',
    ]
    for pattern in patterns:
        m = re.search(pattern, normalized, flags=re.IGNORECASE)
        if m:
            return m.group(0).replace('&amp;', '&')
    return ''


def _build_external_image_proxy_url(image_url: str) -> str:
    return f"/api/external-image?url={quote(str(image_url or '').strip(), safe='')}"


def _build_static_map_fallback_urls(latitude: float | None, longitude: float | None) -> list[str]:
    if latitude is None or longitude is None:
        return []

    return [
        (
            'https://staticmap.openstreetmap.de/staticmap.php'
            f'?center={latitude},{longitude}'
            '&zoom=15&size=800x420&maptype=mapnik'
            f'&markers={latitude},{longitude},red-pushpin'
        ),
        (
            'https://static-maps.yandex.ru/1.x/'
            f'?ll={longitude},{latitude}'
            '&size=650,420&z=15&l=map'
            f'&pt={longitude},{latitude},pm2rdm'
        ),
    ]


def _is_image_response(resp: requests.Response) -> bool:
    content_type = (resp.headers.get('Content-Type') or '').lower()
    return bool(content_type.startswith('image/'))


def _pick_reachable_image_url(candidates: list[str]) -> str:
    for candidate in candidates:
        url = str(candidate or '').strip()
        if not url:
            continue
        try:
            response = requests.get(
                url,
                allow_redirects=True,
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'},
                stream=True,
            )
            if response.status_code < 400 and _is_image_response(response):
                return url
        except requests.RequestException:
            continue
    return ''


def _unwrap_google_redirect_url(url_text: str) -> str:
    text = str(url_text or '').strip()
    if not text:
        return ''
    try:
        parsed = urlparse(text)
        qs = parse_qs(parsed.query)
        for key in ('q', 'url', 'dest', 'destination', 'link'):
            val = (qs.get(key) or [''])[0].strip()
            if val.startswith('http://') or val.startswith('https://'):
                return val
    except Exception:
        return ''
    return ''


def _extract_first_http_url(text: str) -> str:
    raw = str(text or '').strip()
    if not raw:
        return ''

    # Markdown link format: [title](https://...)
    md = re.search(r'\((https?://[^)\s]+)\)', raw)
    if md:
        return md.group(1).strip()

    # Plain URL inside arbitrary text
    direct = re.search(r'(https?://[^\s]+)', raw)
    if direct:
        return direct.group(1).strip()

    return raw


def _is_allowed_google_maps_host(host: str) -> bool:
    value = str(host or '').lower().strip()
    if not value:
        return False

    if value in ('goo.gl', 'g.page', 'maps.app.goo.gl'):
        return True

    # Accept Google domains like google.com, google.co.kr, maps.google.de, etc.
    return 'google.' in value


def _build_place_photo_proxy_url(photo_name: str, max_height: int = 360) -> str:
    safe_height = max(120, min(int(max_height), 1600))
    return f"/api/place-photo?photo_name={quote(photo_name)}&max_height={safe_height}"


def _resolve_maps_photo_url(place_name: str, latitude: float | None, longitude: float | None) -> str:
    text_query = str(place_name or '').strip()
    if not text_query and (latitude is None or longitude is None):
        return ''

    query_candidates = []
    if text_query:
        query_candidates.append(text_query)
        normalized = re.sub(r'\s*\([^)]*\)', '', text_query).strip()
        if normalized and normalized != text_query:
            query_candidates.append(normalized)
    if latitude is not None and longitude is not None:
        query_candidates.append(f'{latitude},{longitude}')

    for query in query_candidates:
        payload = {
            'textQuery': query,
            'pageSize': 5,
            'languageCode': 'ko',
            'rankPreference': 'RELEVANCE',
        }
        if latitude is not None and longitude is not None:
            payload['locationBias'] = {
                'circle': {
                    'center': {
                        'latitude': latitude,
                        'longitude': longitude,
                    },
                    'radius': 1500,
                }
            }

        try:
            body = _call_places_api(payload)
            places = body.get('places', []) or []
            for place in places:
                photos = place.get('photos') or []
                if not photos:
                    continue
                first_photo = photos[0]
                if not isinstance(first_photo, dict):
                    continue
                photo_name = str(first_photo.get('name') or '').strip()
                if photo_name and '/photos/' in photo_name:
                    return _build_place_photo_proxy_url(photo_name, 480)
        except Exception:
            continue

    return ''


def _resolve_coords_from_place_name(place_name: str) -> tuple[float | None, float | None]:
    query = str(place_name or '').strip()
    if not query:
        return None, None

    payload = {
        'textQuery': query,
        'pageSize': 1,
        'languageCode': 'ko',
        'rankPreference': 'RELEVANCE',
    }
    try:
        body = _call_places_api(payload)
        places = body.get('places', []) or []
        if not places:
            return None, None

        loc = (places[0] or {}).get('location') or {}
        lat = _parse_float(loc.get('latitude'))
        lng = _parse_float(loc.get('longitude'))
        return lat, lng
    except Exception:
        return None, None


@app.route('/api/maps-resolve', methods=['POST'])
def resolve_maps_url():
    data = request.get_json(silent=True) or {}
    maps_url = _extract_first_http_url(data.get('maps_url') or '')
    if not maps_url:
        return jsonify({'error': 'maps_url이 필요합니다.'}), 400

    try:
        parsed = urlparse(maps_url)
        host = (parsed.netloc or '').lower()
        if not host:
            return jsonify({'error': '유효한 URL 형식이 아닙니다.'}), 400

        if not _is_allowed_google_maps_host(host):
            return jsonify({'error': 'Google Maps 링크만 지원합니다.'}), 400

        final_url = maps_url
        response_text = ''
        try:
            response = requests.get(
                maps_url,
                allow_redirects=True,
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if response.url:
                final_url = response.url
            response_text = response.text or ''
        except requests.RequestException:
            # Fallback to original URL parsing when redirect resolution fails.
            final_url = maps_url

        unwrapped = _unwrap_google_redirect_url(final_url)
        if unwrapped:
            final_url = unwrapped

        embedded_url = _extract_embedded_maps_url(response_text)
        if embedded_url and not _is_static_map_url(embedded_url):
            final_url = embedded_url

        lat, lng, name = _extract_coords_and_name_from_maps_url(final_url)
        if (lat is None or lng is None) and name:
            fallback_lat, fallback_lng = _resolve_coords_from_place_name(name)
            lat = lat if lat is not None else fallback_lat
            lng = lng if lng is not None else fallback_lng
        photo_url = _resolve_maps_photo_url(name, lat, lng)
        if not photo_url:
            photo_url = _extract_googleusercontent_image_url(response_text)
        if not photo_url:
            photo_url = _extract_meta_image_url(response_text)
        if not photo_url:
            static_candidates = _build_static_map_fallback_urls(lat, lng)
            photo_url = _pick_reachable_image_url(static_candidates)
            if not photo_url and static_candidates:
                photo_url = static_candidates[0]

        if photo_url.startswith('http://') or photo_url.startswith('https://'):
            photo_url = _build_external_image_proxy_url(photo_url)
        return jsonify({
            'input_url': maps_url,
            'final_url': final_url,
            'latitude': lat,
            'longitude': lng,
            'place_name': name,
            'photo_url': photo_url,
            'coord_found': bool(lat is not None and lng is not None),
        }), 200
    except Exception as exc:
        return jsonify({'error': f'링크 해석 실패: {exc}'}), 500


def _collect_places_paginated(base_payload: dict, max_rows: int, max_pages: int) -> tuple[list[dict], int, str]:
    """Collect places across multiple pages (bounded) and return rows/pages/next_token."""
    rows: list[dict] = []
    page_count = 0
    next_token = ''

    for page_index in range(max_pages):
        request_payload = dict(base_payload)
        if next_token:
            request_payload['pageToken'] = next_token

        body = _call_places_api(request_payload)
        page_count += 1

        places = body.get('places', []) or []
        rows.extend(places)

        if len(rows) >= max_rows:
            rows = rows[:max_rows]
            break

        next_token = body.get('nextPageToken', '') or ''
        if not next_token:
            break

        if page_index < max_pages - 1:
            time.sleep(2)

    return rows, page_count, next_token


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/trip-info')
def trip_info_page():
    return render_template('trip_info.html')


@app.route('/trip-feed')
def trip_feed_page():
    return render_template('trip_feed.html')


@app.route('/api/location-info/options', methods=['GET'])
def location_info_options():
    try:
        with _get_mssql_conn() as conn:
            _ensure_location_option_table(conn)
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT country, city
                FROM (
                    SELECT
                        LTRIM(RTRIM(ISNULL([Country], ''))) AS country,
                        LTRIM(RTRIM(ISNULL([City], ''))) AS city
                    FROM dbo.LocationInfo
                    WHERE ISNULL(LTRIM(RTRIM([Country])), '') <> ''
                      AND ISNULL(LTRIM(RTRIM([City])), '') <> ''

                    UNION

                    SELECT
                        LTRIM(RTRIM(ISNULL([Country], ''))) AS country,
                        LTRIM(RTRIM(ISNULL([City], ''))) AS city
                    FROM dbo.location_info_options
                    WHERE ISNULL(LTRIM(RTRIM([Country])), '') <> ''
                      AND ISNULL(LTRIM(RTRIM([City])), '') <> ''
                ) X
                ORDER BY country, city
                '''
            )
            rows = cursor.fetchall()

        cities_by_country: dict[str, list[str]] = {}
        for row in rows:
            country = str(row.country or '').strip()
            city = str(row.city or '').strip()
            if not country or not city:
                continue
            cities = cities_by_country.setdefault(country, [])
            if city not in cities:
                cities.append(city)

        countries = sorted(cities_by_country.keys(), key=lambda item: item.lower())
        for key in list(cities_by_country.keys()):
            cities_by_country[key] = sorted(cities_by_country[key], key=lambda item: item.lower())

        return jsonify({'countries': countries, 'cities_by_country': cities_by_country})
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'콤보 옵션 조회 실패: {exc}'}), 500


@app.route('/api/location-info/options', methods=['POST'])
def create_location_info_option():
    data = request.get_json(silent=True) or {}
    country = str(data.get('country') or '').strip()
    city = str(data.get('city') or '').strip()

    if not country:
        return jsonify({'error': '추가할 국가를 입력하세요.'}), 400
    if not city:
        return jsonify({'error': '추가할 도시를 입력하세요.'}), 400

    try:
        with _get_mssql_conn() as conn:
            _ensure_location_option_table(conn)
            cursor = conn.cursor()
            cursor.execute(
                '''
                IF NOT EXISTS (
                    SELECT 1
                    FROM dbo.location_info_options
                    WHERE LTRIM(RTRIM(Country)) = ?
                      AND LTRIM(RTRIM(City)) = ?
                )
                BEGIN
                    INSERT INTO dbo.location_info_options (Country, City)
                    VALUES (?, ?)
                END
                ''',
                (country, city, country, city),
            )
            conn.commit()

        return jsonify({'message': '국가/도시 옵션이 추가되었습니다.', 'country': country, 'city': city}), 201
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'옵션 추가 실패: {exc}'}), 500


@app.route('/api/location-info/list', methods=['GET'])
def location_info_list():
    country = str(request.args.get('country') or '').strip()
    city = str(request.args.get('city') or '').strip()

    where_clauses = []
    params: list[str] = []
    if country:
        where_clauses.append('LTRIM(RTRIM(ISNULL([Country], \'\'))) = ?')
        params.append(country)
    if city:
        where_clauses.append('LTRIM(RTRIM(ISNULL([City], \'\'))) = ?')
        params.append(city)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ''

    try:
        with _get_mssql_conn() as conn:
            _ensure_location_info_columns(conn)
            cursor = conn.cursor()
            cursor.execute(
                f'''
                SELECT TOP (200)
                    id,
                    LTRIM(RTRIM(ISNULL([Country], ''))) AS country,
                    LTRIM(RTRIM(ISNULL([City], ''))) AS city,
                    LTRIM(RTRIM(ISNULL([Attraction], ''))) AS attraction,
                    ISNULL([Detail], '') AS detail,
                    ISNULL([ImageUrl], '') AS image_url,
                    ISNULL([GoogleMapsUrl], '') AS maps_url,
                    ISNULL([Address], '') AS address,
                    [Latitude] AS latitude,
                    [Longitude] AS longitude,
                    ISNULL([Category], '') AS category
                FROM dbo.LocationInfo
                {where_sql}
                ORDER BY id DESC
                ''',
                tuple(params),
            )
            rows = cursor.fetchall()

        items = []
        for row in rows:
            items.append(
                {
                    'id': _safe_int(row.id),
                    'country': str(row.country or ''),
                    'city': str(row.city or ''),
                    'attraction': str(row.attraction or ''),
                    'detail': str(row.detail or ''),
                    'image_url': str(row.image_url or ''),
                    'maps_url': str(row.maps_url or ''),
                    'address': str(row.address or ''),
                    'latitude': float(row.latitude) if row.latitude is not None else None,
                    'longitude': float(row.longitude) if row.longitude is not None else None,
                    'category': str(row.category or ''),
                }
            )

        return jsonify({'items': items})
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'목록 조회 실패: {exc}'}), 500


@app.route('/api/location-info', methods=['POST'])
def create_location_info():
    data = request.get_json(silent=True) or {}

    country = str(data.get('country') or '').strip()
    city = str(data.get('city') or '').strip()
    attraction = str(data.get('attraction') or '').strip()
    detail = str(data.get('detail') or '').strip()
    image_url = str(data.get('image_url') or '').strip()
    maps_url = str(data.get('maps_url') or '').strip()
    address = str(data.get('address') or '').strip()
    latitude = _parse_float(data.get('latitude'))
    longitude = _parse_float(data.get('longitude'))
    category = str(data.get('category') or '').strip()

    if not country:
        return jsonify({'error': '국가를 선택하세요.'}), 400
    if not city:
        return jsonify({'error': '도시를 선택하세요.'}), 400
    if not attraction:
        return jsonify({'error': 'Attraction을 입력하세요.'}), 400

    try:
        with _get_mssql_conn() as conn:
            _ensure_location_info_columns(conn)
            cursor = conn.cursor()

            cursor.execute(
                '''
                SELECT TOP (1) id
                FROM dbo.LocationInfo
                WHERE LTRIM(RTRIM(ISNULL([Country], ''))) = ?
                  AND LTRIM(RTRIM(ISNULL([City], ''))) = ?
                  AND LTRIM(RTRIM(ISNULL([Attraction], ''))) = ?
                  AND LTRIM(RTRIM(ISNULL([Detail], ''))) = ?
                  AND LTRIM(RTRIM(ISNULL([ImageUrl], ''))) = ?
                  AND LTRIM(RTRIM(ISNULL([GoogleMapsUrl], ''))) = ?
                  AND LTRIM(RTRIM(ISNULL([Address], ''))) = ?
                  AND LTRIM(RTRIM(ISNULL([Category], ''))) = ?
                  AND (([Latitude] IS NULL AND ? IS NULL) OR [Latitude] = ?)
                  AND (([Longitude] IS NULL AND ? IS NULL) OR [Longitude] = ?)
                ORDER BY id DESC
                ''',
                (
                    country,
                    city,
                    attraction,
                    detail,
                    image_url,
                    maps_url,
                    address,
                    category,
                    latitude,
                    latitude,
                    longitude,
                    longitude,
                ),
            )
            existing = cursor.fetchone()
            if existing:
                return jsonify({'message': '동일 데이터가 이미 존재합니다.', 'id': _safe_int(existing[0]), 'duplicate': True}), 200

            cursor.execute(
                '''
                INSERT INTO dbo.LocationInfo (
                    [Country], [City], [Attraction], [Detail], [ImageUrl],
                    [GoogleMapsUrl], [Address], [Latitude], [Longitude], [Category]
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    country,
                    city,
                    attraction,
                    detail or None,
                    image_url or None,
                    maps_url or None,
                    address or None,
                    latitude,
                    longitude,
                    category or None,
                ),
            )

            cursor.execute('SELECT CAST(SCOPE_IDENTITY() AS INT) AS inserted_id')
            inserted = cursor.fetchone()
            conn.commit()

        inserted_id = _safe_int(inserted.inserted_id if inserted else None)
        return jsonify({'message': '저장되었습니다.', 'id': inserted_id}), 201
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'저장 실패: {exc}'}), 500


@app.route('/api/location-info/<int:item_id>', methods=['PUT'])
def update_location_info(item_id: int):
    data = request.get_json(silent=True) or {}

    country = str(data.get('country') or '').strip()
    city = str(data.get('city') or '').strip()
    attraction = str(data.get('attraction') or '').strip()
    detail = str(data.get('detail') or '').strip()
    image_url = str(data.get('image_url') or '').strip()
    maps_url = str(data.get('maps_url') or '').strip()
    address = str(data.get('address') or '').strip()
    latitude = _parse_float(data.get('latitude'))
    longitude = _parse_float(data.get('longitude'))
    category = str(data.get('category') or '').strip()

    if item_id <= 0:
        return jsonify({'error': '잘못된 ID입니다.'}), 400
    if not country:
        return jsonify({'error': '국가를 선택하세요.'}), 400
    if not city:
        return jsonify({'error': '도시를 선택하세요.'}), 400
    if not attraction:
        return jsonify({'error': 'Attraction을 입력하세요.'}), 400

    try:
        with _get_mssql_conn() as conn:
            _ensure_location_info_columns(conn)
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE dbo.LocationInfo
                SET [Country] = ?,
                    [City] = ?,
                    [Attraction] = ?,
                    [Detail] = ?,
                    [ImageUrl] = ?,
                    [GoogleMapsUrl] = ?,
                    [Address] = ?,
                    [Latitude] = ?,
                    [Longitude] = ?,
                    [Category] = ?
                WHERE id = ?
                ''',
                (
                    country,
                    city,
                    attraction,
                    detail or None,
                    image_url or None,
                    maps_url or None,
                    address or None,
                    latitude,
                    longitude,
                    category or None,
                    item_id,
                ),
            )

            if cursor.rowcount == 0:
                conn.rollback()
                return jsonify({'error': '수정할 데이터를 찾지 못했습니다.'}), 404

            conn.commit()

        return jsonify({'message': '수정되었습니다.', 'id': item_id}), 200
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'수정 실패: {exc}'}), 500


@app.route('/api/trips', methods=['GET'])
def list_trips():
    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT t.id,
                       t.title,
                       CONVERT(VARCHAR(10), t.start_date, 23) AS start_date,
                       CONVERT(VARCHAR(10), t.end_date, 23) AS end_date,
                      t.flight_info,
                       CONVERT(VARCHAR(33), t.created_at, 127) AS created_at,
                       ISNULL(pp.place_count, 0) AS place_count,
                       ISNULL(ss.schedule_count, 0) AS schedule_count
                FROM dbo.travel_trips t
                LEFT JOIN (
                    SELECT trip_id, COUNT(*) AS place_count
                    FROM dbo.travel_trip_places
                    GROUP BY trip_id
                ) pp ON pp.trip_id = t.id
                LEFT JOIN (
                    SELECT trip_id, COUNT(*) AS schedule_count
                    FROM dbo.travel_trip_schedule
                    GROUP BY trip_id
                ) ss ON ss.trip_id = t.id
                ORDER BY t.start_date ASC, t.id DESC
                '''
            )
            rows = cursor.fetchall()
        return jsonify({'trips': _rows_to_dicts(cursor, rows)})
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'여행 목록 조회 실패: {exc}'}), 500


@app.route('/api/trips', methods=['POST'])
def create_trip():
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    flight_info = (data.get('flight_info') or '').strip()
    if not title:
        return jsonify({'error': '여행 제목은 필수입니다.'}), 400

    try:
        start_date = _validate_ymd((data.get('start_date') or '').strip())
        end_date = _validate_ymd((data.get('end_date') or '').strip())
    except ValueError:
        return jsonify({'error': '날짜 형식은 YYYY-MM-DD 여야 합니다.'}), 400

    if start_date > end_date:
        return jsonify({'error': '종료일은 시작일보다 빠를 수 없습니다.'}), 400

    try:
        new_id = None
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO dbo.travel_trips (title, start_date, end_date, flight_info)
                VALUES (?, ?, ?, ?)
                ''',
                (title, start_date, end_date, flight_info),
            )
            cursor.execute('SELECT CAST(SCOPE_IDENTITY() AS INT) AS new_id')
            new_id = cursor.fetchone()[0]
            cursor.execute(
                '''
                SELECT id, title,
                       CONVERT(VARCHAR(10), start_date, 23) AS start_date,
                       CONVERT(VARCHAR(10), end_date, 23) AS end_date,
                       flight_info,
                       CONVERT(VARCHAR(33), created_at, 127) AS created_at
                FROM dbo.travel_trips
                WHERE id = ?
                ''',
                (new_id,),
            )
            row = cursor.fetchone()
            trip = _row_to_dict(cursor, row)
        if not trip:
            trip = {
                'id': int(new_id) if new_id is not None else None,
                'title': title,
                'start_date': start_date,
                'end_date': end_date,
                'flight_info': flight_info,
                'created_at': datetime.now().isoformat(),
            }
        return jsonify({'trip': trip}), 201
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'여행 생성 실패: {exc}'}), 500


@app.route('/api/trips/<int:trip_id>', methods=['PUT'])
def update_trip(trip_id: int):
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    flight_info = (data.get('flight_info') or '').strip()
    if not title:
        return jsonify({'error': '여행 제목은 필수입니다.'}), 400

    try:
        start_date = _validate_ymd((data.get('start_date') or '').strip())
        end_date = _validate_ymd((data.get('end_date') or '').strip())
    except ValueError:
        return jsonify({'error': '날짜 형식은 YYYY-MM-DD 여야 합니다.'}), 400

    if start_date > end_date:
        return jsonify({'error': '종료일은 시작일보다 빠를 수 없습니다.'}), 400

    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            cursor = conn.cursor()

            cursor.execute('SELECT id FROM dbo.travel_trips WHERE id = ?', (trip_id,))
            if not cursor.fetchone():
                return jsonify({'error': '수정할 여행이 존재하지 않습니다.'}), 404

            cursor.execute(
                '''
                SELECT COUNT(*)
                FROM dbo.travel_trip_schedule
                WHERE trip_id = ?
                  AND (schedule_date < ? OR schedule_date > ?)
                ''',
                (trip_id, start_date, end_date),
            )
            out_of_range_count = cursor.fetchone()[0]
            if out_of_range_count and int(out_of_range_count) > 0:
                return jsonify({
                    'error': '기존 일정표에 새 기간 밖의 항목이 있습니다. 먼저 일정 날짜를 조정해 주세요.'
                }), 400

            cursor.execute(
                '''
                UPDATE dbo.travel_trips
                SET title = ?, start_date = ?, end_date = ?, flight_info = ?
                WHERE id = ?
                ''',
                (title, start_date, end_date, flight_info, trip_id),
            )

            cursor.execute(
                '''
                SELECT id, title,
                       CONVERT(VARCHAR(10), start_date, 23) AS start_date,
                       CONVERT(VARCHAR(10), end_date, 23) AS end_date,
                       flight_info,
                       CONVERT(VARCHAR(33), created_at, 127) AS created_at
                FROM dbo.travel_trips
                WHERE id = ?
                ''',
                (trip_id,),
            )
            row = cursor.fetchone()
            trip = _row_to_dict(cursor, row)
        if not trip:
            trip = {
                'id': trip_id,
                'title': title,
                'start_date': start_date,
                'end_date': end_date,
                'flight_info': flight_info,
                'created_at': datetime.now().isoformat(),
            }
        return jsonify({'trip': trip}), 200
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'여행 수정 실패: {exc}'}), 500


@app.route('/api/trips/<int:trip_id>', methods=['DELETE'])
def delete_trip(trip_id: int):
    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            cursor = conn.cursor()

            cursor.execute('SELECT id FROM dbo.travel_trips WHERE id = ?', (trip_id,))
            if not cursor.fetchone():
                return jsonify({'error': '삭제할 여행이 존재하지 않습니다.'}), 404

            # 명시적으로 하위 데이터를 먼저 제거해 FK 순서 이슈를 방지한다.
            cursor.execute('DELETE FROM dbo.travel_trip_schedule WHERE trip_id = ?', (trip_id,))
            cursor.execute('DELETE FROM dbo.travel_trip_places WHERE trip_id = ?', (trip_id,))
            cursor.execute('DELETE FROM dbo.travel_trips WHERE id = ?', (trip_id,))
            conn.commit()

        return jsonify({'deleted_id': trip_id}), 200
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'여행 삭제 실패: {exc}'}), 500


@app.route('/api/trip-places', methods=['GET'])
def list_trip_places():
    trip_id_raw = (request.args.get('trip_id') or '').strip()
    if not trip_id_raw:
        return jsonify({'error': 'trip_id는 필수입니다.'}), 400

    try:
        trip_id = int(trip_id_raw)
    except ValueError:
        return jsonify({'error': 'trip_id는 숫자여야 합니다.'}), 400

    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            cursor = conn.cursor()
            cursor.execute(
                '''
                  SELECT p.id, p.trip_id, p.place_name,
                      COALESCE(NULLIF(LTRIM(RTRIM(p.place_category)), ''), 'point_of_interest') AS place_category,
                      p.formatted_address,
                       p.google_maps_uri,
                       p.latitude,
                       p.longitude,
                       p.memo,
                       CONVERT(VARCHAR(33), p.created_at, 127) AS created_at
                FROM dbo.travel_trip_places p
                WHERE p.trip_id = ?
                ORDER BY p.id DESC
                ''',
                (trip_id,),
            )
            rows = cursor.fetchall()
        return jsonify({'places': _rows_to_dicts(cursor, rows)})
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'여행 장소 조회 실패: {exc}'}), 500


@app.route('/api/trip-places', methods=['POST'])
def create_trip_place():
    data = request.get_json(silent=True) or {}
    place_name = (data.get('place_name') or '').strip()
    if not place_name:
        return jsonify({'error': '장소 이름은 필수입니다.'}), 400

    try:
        trip_id = int(data.get('trip_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'trip_id는 숫자여야 합니다.'}), 400

    formatted_address = (data.get('formatted_address') or '').strip()
    google_maps_uri = (data.get('google_maps_uri') or '').strip()
    latitude = _parse_float(data.get('latitude'))
    longitude = _parse_float(data.get('longitude'))
    memo = _fit_db_text((data.get('memo') or '').strip())
    place_category = (data.get('place_category') or '').strip().lower()

    allowed_categories = {
        'tourist_attraction',
        'historical_landmark',
        'restaurant',
        'cafe',
        'lodging',
        'museum',
        'park',
        'point_of_interest',
    }
    if place_category and place_category not in allowed_categories:
        place_category = ''

    if not place_category:
        # Default legacy/unknown category to tourist bucket instead of leaving it empty.
        place_category = 'point_of_interest'

    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM dbo.travel_trips WHERE id = ?', (trip_id,))
            if not cursor.fetchone():
                return jsonify({'error': '선택한 여행이 존재하지 않습니다.'}), 404

            cursor.execute(
                '''
                INSERT INTO dbo.travel_trip_places (trip_id, place_name, place_category, formatted_address, google_maps_uri, latitude, longitude, memo)
                OUTPUT
                    INSERTED.id,
                    INSERTED.trip_id,
                    INSERTED.place_name,
                    INSERTED.place_category,
                    INSERTED.formatted_address,
                    INSERTED.google_maps_uri,
                    INSERTED.latitude,
                    INSERTED.longitude,
                    INSERTED.memo,
                    CONVERT(VARCHAR(33), INSERTED.created_at, 127) AS created_at
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (trip_id, place_name, place_category, formatted_address, google_maps_uri, latitude, longitude, memo),
            )
            row = cursor.fetchone()
            place = _row_to_dict(cursor, row)
        if not place:
            return jsonify({'error': '여행 장소 저장 후 조회에 실패했습니다.'}), 500
        return jsonify({'place': place}), 201
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'여행 장소 저장 실패: {exc}'}), 500


@app.route('/api/trip-places/<int:place_id>', methods=['PUT'])
def update_trip_place(place_id: int):
    data = request.get_json(silent=True) or {}

    allowed_categories = {
        'tourist_attraction',
        'historical_landmark',
        'restaurant',
        'cafe',
        'lodging',
        'museum',
        'park',
        'point_of_interest',
    }

    raw_lat = data.get('latitude')
    raw_lng = data.get('longitude')

    has_lat_key = 'latitude' in data
    has_lng_key = 'longitude' in data
    if has_lat_key != has_lng_key:
        return jsonify({'error': '위도와 경도는 함께 입력하거나 함께 비워야 합니다.'}), 400

    has_lat = raw_lat is not None and str(raw_lat).strip() != ''
    has_lng = raw_lng is not None and str(raw_lng).strip() != ''

    if has_lat_key and has_lng_key and has_lat != has_lng:
        return jsonify({'error': '위도와 경도는 함께 입력하거나 함께 비워야 합니다.'}), 400

    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            cursor = conn.cursor()

            cursor.execute(
                '''
                SELECT id, trip_id, place_name,
                       COALESCE(NULLIF(LTRIM(RTRIM(place_category)), ''), 'point_of_interest') AS place_category,
                       formatted_address,
                       google_maps_uri,
                       latitude,
                       longitude,
                       memo
                FROM dbo.travel_trip_places
                WHERE id = ?
                ''',
                (place_id,),
            )
            existing_row = cursor.fetchone()
            existing = _row_to_dict(cursor, existing_row) if existing_row else None
            if not existing:
                return jsonify({'error': '수정할 장소가 존재하지 않습니다.'}), 404

            place_name = str(existing.get('place_name') or '').strip()
            if 'place_name' in data:
                place_name = (data.get('place_name') or '').strip()
            if not place_name:
                return jsonify({'error': '장소 이름은 필수입니다.'}), 400

            place_category = str(existing.get('place_category') or 'point_of_interest').strip().lower()
            if 'place_category' in data:
                place_category = (data.get('place_category') or '').strip().lower()
            if place_category and place_category not in allowed_categories:
                place_category = ''
            if not place_category:
                place_category = 'point_of_interest'

            formatted_address = str(existing.get('formatted_address') or '').strip()
            if 'formatted_address' in data:
                formatted_address = (data.get('formatted_address') or '').strip()

            google_maps_uri = str(existing.get('google_maps_uri') or '').strip()
            if 'google_maps_uri' in data:
                google_maps_uri = (data.get('google_maps_uri') or '').strip()

            latitude = existing.get('latitude')
            longitude = existing.get('longitude')
            if has_lat_key and has_lng_key:
                if has_lat and has_lng:
                    try:
                        latitude = float(raw_lat)
                        longitude = float(raw_lng)
                    except (TypeError, ValueError):
                        return jsonify({'error': '위도/경도는 숫자여야 합니다.'}), 400

                    if latitude < -90 or latitude > 90:
                        return jsonify({'error': '위도 범위는 -90~90 입니다.'}), 400
                    if longitude < -180 or longitude > 180:
                        return jsonify({'error': '경도 범위는 -180~180 입니다.'}), 400
                else:
                    latitude = None
                    longitude = None

            cursor.execute(
                '''
                UPDATE dbo.travel_trip_places
                SET place_name = ?,
                    place_category = ?,
                    formatted_address = ?,
                    google_maps_uri = ?,
                    latitude = ?,
                    longitude = ?
                WHERE id = ?
                ''',
                (place_name, place_category, formatted_address, google_maps_uri, latitude, longitude, place_id),
            )

            cursor.execute(
                '''
                SELECT id, trip_id, place_name,
                       COALESCE(NULLIF(LTRIM(RTRIM(place_category)), ''), 'point_of_interest') AS place_category,
                       formatted_address,
                       google_maps_uri,
                       latitude,
                       longitude,
                       memo,
                       CONVERT(VARCHAR(33), created_at, 127) AS created_at
                FROM dbo.travel_trip_places
                WHERE id = ?
                ''',
                (place_id,),
            )
            row = cursor.fetchone()
            updated = _row_to_dict(cursor, row)
            conn.commit()

        if not updated:
            return jsonify({'error': '장소 정보 수정 후 조회에 실패했습니다.'}), 500
        return jsonify({'place': updated}), 200
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'장소 정보 수정 실패: {exc}'}), 500


@app.route('/api/trip-places/<int:place_id>', methods=['DELETE'])
def delete_trip_place(place_id: int):
    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            cursor = conn.cursor()

            cursor.execute('SELECT id, trip_id, place_name FROM dbo.travel_trip_places WHERE id = ?', (place_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'error': '삭제할 장소가 존재하지 않습니다.'}), 404

            cursor.execute('DELETE FROM dbo.travel_trip_places WHERE id = ?', (place_id,))
            conn.commit()

        return jsonify({
            'deleted': True,
            'place_id': int(row[0]),
            'trip_id': int(row[1]),
            'place_name': str(row[2] or ''),
        }), 200
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'장소 삭제 실패: {exc}'}), 500


@app.route('/api/trip-schedule', methods=['POST'])
def create_trip_schedule():
    data = request.get_json(silent=True) or {}

    try:
        trip_id = int(data.get('trip_id'))
        trip_place_id = int(data.get('trip_place_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'trip_id와 trip_place_id는 숫자여야 합니다.'}), 400

    try:
        schedule_date = _validate_ymd((data.get('schedule_date') or '').strip())
    except ValueError:
        return jsonify({'error': 'schedule_date 형식은 YYYY-MM-DD 여야 합니다.'}), 400

    try:
        schedule_time = _validate_hhmm((data.get('schedule_time') or '10:00').strip())
    except ValueError:
        return jsonify({'error': 'schedule_time 형식은 HH:MM 이어야 합니다. 예: 10:00'}), 400

    note = _fit_db_text((data.get('note') or '').strip())

    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            has_schedule_time = _has_schedule_time_column(conn)
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT id,
                       CONVERT(VARCHAR(10), start_date, 23) AS start_date,
                       CONVERT(VARCHAR(10), end_date, 23) AS end_date
                FROM dbo.travel_trips
                WHERE id = ?
                ''',
                (trip_id,),
            )
            trip = cursor.fetchone()
            if not trip:
                return jsonify({'error': '선택한 여행이 존재하지 않습니다.'}), 404

            start_date = trip[1]
            end_date = trip[2]
            if schedule_date < start_date or schedule_date > end_date:
                return jsonify({'error': '선택 날짜가 여행 기간을 벗어났습니다.'}), 400

            cursor.execute(
                '''
                SELECT id, place_name, formatted_address, google_maps_uri,
                       latitude, longitude,
                       ISNULL(memo, '') AS memo
                FROM dbo.travel_trip_places
                WHERE id = ? AND trip_id = ?
                ''',
                (trip_place_id, trip_id),
            )
            place_row = cursor.fetchone()
            if not place_row:
                return jsonify({'error': '선택 장소가 해당 여행에 속하지 않습니다.'}), 400

            place_name = place_row[1]
            formatted_address = place_row[2]
            google_maps_uri = place_row[3]
            place_latitude = place_row[4]
            place_longitude = place_row[5]
            place_memo = str(place_row[6] or '').strip()
            if not note and place_memo:
                note = _fit_db_text(place_memo)

            if has_schedule_time:
                cursor.execute(
                    '''
                    INSERT INTO dbo.travel_trip_schedule (trip_id, schedule_date, schedule_time, trip_place_id, note)
                    VALUES (?, ?, ?, ?, ?)
                    ''',
                    (trip_id, schedule_date, schedule_time, trip_place_id, note),
                )
            else:
                cursor.execute(
                    '''
                    INSERT INTO dbo.travel_trip_schedule (trip_id, schedule_date, trip_place_id, note)
                    VALUES (?, ?, ?, ?)
                    ''',
                    (trip_id, schedule_date, trip_place_id, note),
                )
            cursor.execute('SELECT CAST(SCOPE_IDENTITY() AS INT) AS new_id')
            identity_row = cursor.fetchone()
            new_id = int(identity_row[0]) if identity_row and identity_row[0] is not None else None
            conn.commit()

        schedule = {
            'id': new_id,
            'trip_id': trip_id,
            'schedule_date': schedule_date,
            'schedule_time': schedule_time,
            'trip_place_id': trip_place_id,
            'place_name': place_name,
            'formatted_address': formatted_address,
            'google_maps_uri': google_maps_uri,
            'latitude': place_latitude,
            'longitude': place_longitude,
            'note': note,
        }
        return jsonify({'schedule': schedule}), 201
    except pyodbc.IntegrityError:
        return jsonify({'error': '같은 날짜에 같은 장소가 이미 등록되어 있습니다.'}), 409
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'일정 배정 실패: {exc}'}), 500


@app.route('/api/trip-schedule', methods=['GET'])
def list_trip_schedule():
    trip_id_raw = (request.args.get('trip_id') or '').strip()
    schedule_date = (request.args.get('schedule_date') or '').strip()

    if not trip_id_raw:
        return jsonify({'error': 'trip_id는 필수입니다.'}), 400

    try:
        trip_id = int(trip_id_raw)
    except ValueError:
        return jsonify({'error': 'trip_id는 숫자여야 합니다.'}), 400

    if schedule_date:
        try:
            schedule_date = _validate_ymd(schedule_date)
        except ValueError:
            return jsonify({'error': 'schedule_date 형식은 YYYY-MM-DD 여야 합니다.'}), 400

    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            has_schedule_time = _has_schedule_time_column(conn)
            cursor = conn.cursor()
            if schedule_date:
                if has_schedule_time:
                    cursor.execute(
                        '''
                        SELECT s.id, s.trip_id,
                               CONVERT(VARCHAR(10), s.schedule_date, 23) AS schedule_date,
                               ISNULL(s.schedule_time, '10:00') AS schedule_time,
                               s.trip_place_id,
                               p.place_name,
                               p.formatted_address,
                               p.google_maps_uri,
                               p.latitude,
                               p.longitude,
                               s.note,
                               CONVERT(VARCHAR(33), s.created_at, 127) AS created_at
                        FROM dbo.travel_trip_schedule s
                        JOIN dbo.travel_trip_places p ON p.id = s.trip_place_id
                        WHERE s.trip_id = ? AND s.schedule_date = ?
                        ORDER BY s.id ASC
                        ''',
                        (trip_id, schedule_date),
                    )
                else:
                    cursor.execute(
                        '''
                        SELECT s.id, s.trip_id,
                               CONVERT(VARCHAR(10), s.schedule_date, 23) AS schedule_date,
                               '10:00' AS schedule_time,
                               s.trip_place_id,
                               p.place_name,
                               p.formatted_address,
                               p.google_maps_uri,
                               p.latitude,
                               p.longitude,
                               s.note,
                               CONVERT(VARCHAR(33), s.created_at, 127) AS created_at
                        FROM dbo.travel_trip_schedule s
                        JOIN dbo.travel_trip_places p ON p.id = s.trip_place_id
                        WHERE s.trip_id = ? AND s.schedule_date = ?
                        ORDER BY s.id ASC
                        ''',
                        (trip_id, schedule_date),
                    )
            else:
                if has_schedule_time:
                    cursor.execute(
                        '''
                        SELECT s.id, s.trip_id,
                               CONVERT(VARCHAR(10), s.schedule_date, 23) AS schedule_date,
                               ISNULL(s.schedule_time, '10:00') AS schedule_time,
                               s.trip_place_id,
                               p.place_name,
                               p.formatted_address,
                               p.google_maps_uri,
                               p.latitude,
                               p.longitude,
                               s.note,
                               CONVERT(VARCHAR(33), s.created_at, 127) AS created_at
                        FROM dbo.travel_trip_schedule s
                        JOIN dbo.travel_trip_places p ON p.id = s.trip_place_id
                        WHERE s.trip_id = ?
                        ORDER BY s.schedule_date ASC, s.id ASC
                        ''',
                        (trip_id,),
                    )
                else:
                    cursor.execute(
                        '''
                        SELECT s.id, s.trip_id,
                               CONVERT(VARCHAR(10), s.schedule_date, 23) AS schedule_date,
                               '10:00' AS schedule_time,
                               s.trip_place_id,
                               p.place_name,
                               p.formatted_address,
                               p.google_maps_uri,
                               p.latitude,
                               p.longitude,
                               s.note,
                               CONVERT(VARCHAR(33), s.created_at, 127) AS created_at
                        FROM dbo.travel_trip_schedule s
                        JOIN dbo.travel_trip_places p ON p.id = s.trip_place_id
                        WHERE s.trip_id = ?
                        ORDER BY s.schedule_date ASC, s.id ASC
                        ''',
                        (trip_id,),
                    )
            rows = cursor.fetchall()
            schedule_items = _rows_to_dicts(cursor, rows)

            cursor.execute(
                '''
                SELECT CONVERT(VARCHAR(10), start_date, 23) AS start_date,
                       CONVERT(VARCHAR(10), end_date, 23) AS end_date
                FROM dbo.travel_trips
                WHERE id = ?
                ''',
                (trip_id,),
            )
            trip_row = cursor.fetchone()
            if not trip_row:
                return jsonify({'error': '선택한 여행이 존재하지 않습니다.'}), 404

            day_rows = _iter_trip_days(trip_row[0], trip_row[1])

        return jsonify({'days': day_rows, 'schedule': schedule_items})
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'일정 조회 실패: {exc}'}), 500


@app.route('/api/trip-schedule/<int:schedule_id>', methods=['PUT'])
def update_trip_schedule(schedule_id: int):
    data = request.get_json(silent=True) or {}

    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            has_schedule_time = _has_schedule_time_column(conn)
            cursor = conn.cursor()

            cursor.execute(
                '''
                SELECT s.id,
                       s.trip_id,
                       s.trip_place_id,
                      CONVERT(VARCHAR(10), s.schedule_date, 23) AS current_schedule_date,
                      ISNULL(s.schedule_time, '10:00') AS current_schedule_time,
                      ISNULL(s.note, '') AS current_note,
                       CONVERT(VARCHAR(10), t.start_date, 23) AS start_date,
                       CONVERT(VARCHAR(10), t.end_date, 23) AS end_date
                FROM dbo.travel_trip_schedule s
                JOIN dbo.travel_trips t ON t.id = s.trip_id
                WHERE s.id = ?
                ''',
                (schedule_id,),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({'error': '수정할 일정이 존재하지 않습니다.'}), 404

            trip_id = int(row[1])
            trip_place_id = int(row[2])
            current_schedule_date = str(row[3] or '')
            current_schedule_time = str(row[4] or '10:00')
            current_note = str(row[5] or '')
            start_date = row[6]
            end_date = row[7]

            raw_schedule_date = data.get('schedule_date')
            if raw_schedule_date is None or str(raw_schedule_date).strip() == '':
                schedule_date = current_schedule_date
            else:
                try:
                    schedule_date = _validate_ymd(str(raw_schedule_date).strip())
                except ValueError:
                    return jsonify({'error': 'schedule_date 형식은 YYYY-MM-DD 여야 합니다.'}), 400

            raw_schedule_time = data.get('schedule_time')
            if raw_schedule_time is None or str(raw_schedule_time).strip() == '':
                schedule_time = current_schedule_time
            else:
                try:
                    schedule_time = _validate_hhmm(str(raw_schedule_time).strip())
                except ValueError:
                    return jsonify({'error': 'schedule_time 형식은 HH:MM 이어야 합니다. 예: 10:00'}), 400

            note = current_note
            if 'note' in data:
                note = _fit_db_text(str(data.get('note') or '').strip())

            if schedule_date < start_date or schedule_date > end_date:
                return jsonify({'error': '선택 날짜가 여행 기간을 벗어났습니다.'}), 400

            if has_schedule_time:
                cursor.execute(
                    '''
                    UPDATE dbo.travel_trip_schedule
                    SET schedule_date = ?,
                        schedule_time = ?,
                        note = ?
                    WHERE id = ?
                    ''',
                    (schedule_date, schedule_time, note, schedule_id),
                )
            else:
                cursor.execute(
                    '''
                    UPDATE dbo.travel_trip_schedule
                    SET schedule_date = ?,
                        note = ?
                    WHERE id = ?
                    ''',
                    (schedule_date, note, schedule_id),
                )

            place_memo = _fit_db_text(_extract_user_memo_text(note))

            cursor.execute(
                '''
                UPDATE dbo.travel_trip_places
                SET memo = ?
                WHERE id = ?
                ''',
                (place_memo, trip_place_id),
            )

            cursor.execute(
                '''
                SELECT s.id, s.trip_id,
                       CONVERT(VARCHAR(10), s.schedule_date, 23) AS schedule_date,
                       ISNULL(s.schedule_time, '10:00') AS schedule_time,
                       s.trip_place_id,
                       p.place_name,
                       p.formatted_address,
                       p.google_maps_uri,
                      p.latitude,
                      p.longitude,
                       s.note,
                       CONVERT(VARCHAR(33), s.created_at, 127) AS created_at
                FROM dbo.travel_trip_schedule s
                JOIN dbo.travel_trip_places p ON p.id = s.trip_place_id
                WHERE s.id = ?
                ''',
                (schedule_id,),
            )
            updated_row = cursor.fetchone()
            updated = _row_to_dict(cursor, updated_row)
            conn.commit()

        if not updated:
            return jsonify({'error': '일정 수정 후 조회에 실패했습니다.'}), 500

        return jsonify({'schedule': updated, 'trip_id': trip_id, 'trip_place_id': trip_place_id}), 200
    except pyodbc.IntegrityError:
        return jsonify({'error': '해당 날짜/시간에 같은 장소 일정이 이미 있습니다.'}), 409
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'일정 수정 실패: {exc}'}), 500


@app.route('/api/trip-schedule/<int:schedule_id>', methods=['DELETE'])
def delete_trip_schedule(schedule_id: int):
    try:
        with _get_mssql_conn() as conn:
            _ensure_planner_tables(conn)
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT id, trip_id, trip_place_id
                FROM dbo.travel_trip_schedule
                WHERE id = ?
                ''',
                (schedule_id,),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({'error': '삭제할 일정이 존재하지 않습니다.'}), 404

            trip_id = int(row[1])
            trip_place_id = int(row[2])

            cursor.execute('DELETE FROM dbo.travel_trip_schedule WHERE id = ?', (schedule_id,))
            conn.commit()

        return jsonify({'deleted_id': schedule_id, 'trip_id': trip_id, 'trip_place_id': trip_place_id}), 200
    except (RuntimeError, pyodbc.Error) as exc:
        return jsonify({'error': f'일정 삭제 실패: {exc}'}), 500


@app.route('/api/search', methods=['POST'])
def search_places():
    try:
        data = request.get_json(silent=True) or {}
        payload = _build_payload(data)

        max_rows_raw = data.get('max_rows', 50)
        try:
            max_rows = int(max_rows_raw)
        except (TypeError, ValueError):
            max_rows = 50
        max_rows = max(10, min(max_rows, 100))

        max_pages = max(1, min((max_rows + 19) // 20, 5))
        raw_places, pages, next_token = _collect_places_paginated(payload, max_rows=max_rows, max_pages=max_pages)
        places = _normalize_places(raw_places)
        return jsonify({'places': places, 'next_page_token': next_token, 'pages': pages})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 500
    except requests.RequestException as exc:
        return jsonify({'error': f'외부 API 호출 실패: {exc}'}), 502


@app.route('/api/search_table', methods=['POST'])
def search_places_table():
    """Fetch several pages from Places API and return flattened rows for table rendering."""
    try:
        data = request.get_json(silent=True) or {}
        payload = _build_payload(data)

        max_pages_raw = data.get('max_pages', 3)
        try:
            max_pages = int(max_pages_raw)
        except (TypeError, ValueError):
            max_pages = 3
        max_pages = max(1, min(max_pages, 5))

        rows: list[dict] = []
        page_count = 0
        next_token = ''

        for page_index in range(max_pages):
            request_payload = dict(payload)
            if next_token:
                request_payload['pageToken'] = next_token

            body = _call_places_api(request_payload)
            page_count += 1
            places = body.get('places', []) or []
            rows.extend(_flatten_place_for_table(place) for place in places)

            next_token = body.get('nextPageToken', '') or ''
            if not next_token:
                break

            # nextPageToken이 활성화되기까지 짧은 대기 필요
            if page_index < max_pages - 1:
                time.sleep(2)

        columns = [
            'name', 'resource_name', 'primary_type', 'types', 'rating', 'user_rating_count',
            'price_level', 'business_status', 'formatted_address', 'latitude', 'longitude',
            'phone', 'website', 'google_maps', 'photo_count'
        ]

        field_presence = {
            col: sum(1 for row in rows if row.get(col) not in (None, '', []))
            for col in columns
        }

        return jsonify({
            'summary': {
                'fetched_rows': len(rows),
                'fetched_pages': page_count,
                'has_more': bool(next_token),
                'field_presence': field_presence,
            },
            'columns': columns,
            'rows': rows,
        })
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 500
    except requests.RequestException as exc:
        return jsonify({'error': f'외부 API 호출 실패: {exc}'}), 502


@app.route('/api/raw_json_100', methods=['GET'])
def raw_json_100():
    """Return up to 100 raw place JSON items without requiring user search input."""
    try:
        country_code = (request.args.get('country_code') or '').strip().lower()
        if country_code and (len(country_code) != 2 or not country_code.isalpha()):
            return jsonify({'error': 'country_code는 2자리 영문이어야 합니다. 예: kr, fr, jp'}), 400

        base_payload = {
            'textQuery': '인기 관광 명소 추천',
            'pageSize': 20,
            'languageCode': 'ko',
            'rankPreference': 'RELEVANCE',
        }
        if country_code:
            base_payload['regionCode'] = country_code

        max_rows = 100
        max_pages = 10
        rows: list[dict] = []
        page_count = 0
        next_token = ''

        for page_index in range(max_pages):
            request_payload = dict(base_payload)
            if next_token:
                request_payload['pageToken'] = next_token

            body = _call_places_api(request_payload)
            page_count += 1
            places = body.get('places', []) or []
            rows.extend(places)

            if len(rows) >= max_rows:
                rows = rows[:max_rows]
                break

            next_token = body.get('nextPageToken', '') or ''
            if not next_token:
                break

            if page_index < max_pages - 1:
                time.sleep(2)

        return jsonify({
            'summary': {
                'rows': len(rows),
                'pages': page_count,
                'has_more': bool(next_token),
                'country_code': country_code or 'all',
            },
            'places': rows,
        })
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 500
    except requests.RequestException as exc:
        return jsonify({'error': f'외부 API 호출 실패: {exc}'}), 502


@app.route('/api/place-photo')
def place_photo():
    """Proxy Places Photo media so API key is kept server-side."""
    photo_name = (request.args.get('photo_name') or '').strip()
    if not photo_name or '/photos/' not in photo_name:
        return jsonify({'error': '유효한 photo_name이 필요합니다.'}), 400

    places_api_key = _get_places_api_key()
    if not places_api_key:
        return jsonify({'error': 'GOOGLE_PLACES_API_KEY가 설정되지 않았습니다.'}), 500

    max_height_raw = request.args.get('max_height', '360')
    try:
        max_height = max(120, min(int(max_height_raw), 1600))
    except ValueError:
        max_height = 360

    photo_url = f'https://places.googleapis.com/v1/{photo_name}/media'

    try:
        response = requests.get(
            photo_url,
            headers={'X-Goog-Api-Key': places_api_key},
            params={'maxHeightPx': max_height},
            timeout=20,
        )
    except requests.RequestException as exc:
        return jsonify({'error': f'사진 조회 실패: {exc}'}), 502

    if response.status_code >= 400:
        try:
            message = response.json().get('error', {}).get('message')
        except ValueError:
            message = response.text
        return jsonify({'error': f'사진 API 오류: {message or response.status_code}'}), response.status_code

    content_type = response.headers.get('Content-Type', 'image/jpeg')
    return Response(response.content, mimetype=content_type)


@app.route('/api/external-image')
def external_image_proxy():
    source_url = (request.args.get('url') or '').strip()
    if not source_url:
        return jsonify({'error': 'url 파라미터가 필요합니다.'}), 400

    parsed = urlparse(source_url)
    if parsed.scheme not in ('http', 'https'):
        return jsonify({'error': 'http/https URL만 지원합니다.'}), 400

    try:
        response = requests.get(
            source_url,
            allow_redirects=True,
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
    except requests.RequestException as exc:
        return jsonify({'error': f'외부 이미지 조회 실패: {exc}'}), 502

    if response.status_code >= 400:
        return jsonify({'error': f'외부 이미지 응답 오류: {response.status_code}'}), response.status_code

    content_type = response.headers.get('Content-Type', 'application/octet-stream')
    if not content_type.lower().startswith('image/'):
        return jsonify({'error': '이미지 응답이 아닙니다.'}), 415

    return Response(response.content, mimetype=content_type)


if __name__ == '__main__':
    debug = (os.getenv('FLASK_DEBUG', 'true').strip().lower() == 'true')
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=debug)
