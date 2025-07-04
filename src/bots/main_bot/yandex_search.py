"""
Оптимизированный асинхронный клиент для Yandex Search API

Copyright (c) 2025 Fedor Kondakov, MIT License
"""

import asyncio
import base64
import aiohttp
import logging
from typing import List, Dict, Optional
import re
from xml.etree import ElementTree as ET

##########################
## ГЛОБАЛЬНЫЕ НАСТРОЙКИ ##
##########################

main_logger = logging.getLogger("YandexSearch")
parser_logger = logging.getLogger("YandexSearch.Parser")

if not main_logger.handlers:
    main_logger.addHandler(logging.NullHandler())
if not parser_logger.handlers:
    parser_logger.addHandler(logging.NullHandler())

#################
## ПАРСИНГ XML ##
#################

# Пространства имен XML для обработки элементов Yandex Search API
XML_NAMESPACES = {
    'yandex': 'http://api.yandex.ru/xmldoc/'
}

def _get_element_full_text(element: Optional[ET.Element]) -> str:
    """
    Description:
    ---------------
        Рекурсивно извлекает весь текст из XML-элемента и его дочерних элементов,
        объединяя текстовые узлы в единую строку.
        
        Эффективно обрабатывает элементы с вложенными тегами, сохраняя их текстовое содержимое.
        
    Args:
    ---------------
        element (Optional[ET.Element]): 
            XML-элемент для обработки. Если None, возвращает пустую строку.
            
    Returns:
    ---------------
        str: 
            Объединенный текст элемента и его потомков, очищенный от лишних пробелов.
            Пустая строка, если элемент отсутствует.
    """
    return ''.join(element.itertext()).strip() if element is not None else ""

def _clean_text(text: str) -> str:
    """
    Description:
    ---------------
        Очищает текст от HTML/XML-тегов и лишних пробелов.
        Оптимизирована для быстрой обработки текстовых данных.
        
        Особенности:
           - Быстро возвращает текст без изменений, если в нем нет символов '<' и '>'
           - Использует эффективное регулярное выражение для удаления тегов
           - Удаляет начальные и конечные пробелы
        
    Args:
    ---------------
        text (str): 
            Исходный текст с возможным HTML/XML-форматированием.
            
    Returns:
    ---------------
        str: 
            Текст без тегов с нормализованными пробелами.
            
    Examples:
    ---------------
        >>> clean_text("<b>Hello</b>   World")
        "Hello World"
    """
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()

def _parse_search_results(xml_content: str) -> List[Dict]:
    """
    Description:
    ---------------
        Парсит XML-ответ Yandex Search API в структурированные словари.
        Оптимизированная версия с улучшенной производительностью и обработкой ошибок.
        
        Ключевые особенности:
           - Использует потоковый парсинг через iter() для экономии памяти
           - Объединенная логика извлечения контента по приоритетам
           - Подробное логирование процесса обработки
           - Устойчивость к ошибкам формата XML
        
    Args:
    ---------------
        xml_content (str): 
            Строка с XML-контентом, полученным от Yandex Search API.
            
    Returns:
    ---------------
        List[Dict]:
            Список словарей с распарсенными результатами. Каждый словарь содержит:
               - url: URL страницы
               - domain: Домен сайта
               - title: Заголовок страницы
               - headline: Краткое описание
               - modtime: Время последнего обновления (YYYYMMDDThhmmss)
               - lang: Язык контента
               - content: Основное текстовое содержимое
            
        Пустой список, если возникли ошибки парсинга.
        
    Processing Logic:
    ---------------
        1. Извлечение базовых метаданных (url, domain, title и т.д.)
        2. Определение основного контента по приоритетам:
           a) extended-text из свойств документа
           b) Пассажи (passages) из тела документа
           c) Заголовок (headline), если другие варианты недоступны
        3. Очистка и нормализация всех текстовых полей
        4. Обработка каждого документа в изолированном try/except блоке
    """
    parser_logger.debug("Начало парсинга XML результатов поиска")
    try:
        # Парсинг XML-контента в дерево элементов
        root = ET.fromstring(xml_content)
        parsed_results = []
        total_docs = 0
        processed_docs = 0
        
        # Итерация по всем элементам <doc> в XML
        for doc_elem in root.iter('doc'):
            total_docs += 1
            try:
                # Извлечение основных метаданных
                url = doc_elem.findtext('url', '')
                domain = doc_elem.findtext('domain', '')
                title = _clean_text(_get_element_full_text(doc_elem.find('title')))
                headline = _clean_text(_get_element_full_text(doc_elem.find('headline')))
                modtime = doc_elem.findtext('modtime', '')
                
                # Обработка свойств документа (включая язык)
                properties_elem = doc_elem.find('properties')
                lang = properties_elem.findtext('lang', '') if properties_elem else ''
                
                # Определение основного контента по приоритетам
                content = ""
                
                # Приоритет 1: extended-text (расширенное описание)
                if properties_elem:
                    ext_elem = properties_elem.find('extended-text')
                    if ext_elem is not None:
                        content = _get_element_full_text(ext_elem)
                
                # Приоритет 2: passages (текстовые фрагменты документа)
                if not content:
                    passages = []
                    for passage_elem in doc_elem.iter('passage'):
                        passage_text = _get_element_full_text(passage_elem)
                        if passage_text:
                            passages.append(passage_text)
                    content = " ".join(passages)
                
                # Приоритет 3: headline (если другие источники пусты)
                if not content and headline:
                    content = headline
                
                # Формирование результата
                parsed_results.append({
                    "url": url,
                    "domain": domain,
                    "title": title,
                    "headline": headline,
                    "modtime": modtime,
                    "lang": lang,
                    "content": _clean_text(content)
                })
                processed_docs += 1
                
            except Exception as doc_error:
                parser_logger.error(f"Ошибка обработки документа #{total_docs}: {str(doc_error)}")
                continue
        
        parser_logger.info(f"Успешно обработано документов: {processed_docs}/{total_docs}")
        return parsed_results
    
    except ET.ParseError as parse_error:
        parser_logger.error(f"Ошибка парсинга XML: {str(parse_error)}")
        return []
    except Exception as critical_error:
        parser_logger.exception(f"Критическая ошибка при обработке XML: {str(critical_error)}")
        return []

####################################
## ОПТИМИЗАЦИЯ РЕЗУЛЬТАТОВ ПОИСКА ##
####################################

def optimize_results(
    parsed_results: List[Dict],
    min_length: int = 0,
    max_length: int = float('inf')
) -> List[Dict]:
    """
    Description:
    ---------------
        Фильтрует результаты поиска для использования в языковых моделях:
           - Убирает результаты без контента
           - Удаляет слишком короткие или длинные фрагменты
    Args:
    ---------------
        parsed_results (list[dict]): Сырые результаты парсинга
        min_length (Optional[int]): Минимальная длина контента
        max_length (Optional[int]): Максимальная длина контента
    Returns:
    ---------------
        list[dict]: Оптимизированные результаты поиска
    """
    parser_logger.debug("Фильтрация результатов.")

    filtered = [
        item for item in parsed_results
        if item.get("content") and max_length > len(item["content"]) > min_length
    ]

    filtered_count = len(parsed_results) - len(filtered)

    if filtered_count:
        parser_logger.info(f"Отфильтровано результатов: {filtered_count}")

    return filtered

def format_results(results: List[Dict], query: str) -> str:
    """
    Description:
    ---------------
        Форматирует результаты поиска для передачи в языковую модель
    Args:
    ---------------
        results (List[Dict]): Оптимизированные результаты поиска
        query (str): Исходный поисковый запрос
    Returns:
    ---------------
        str: Отформатированная строка с результатами
    Examples:
    ---------------
        format_results([...], "Python") -> 
        "Результаты поиска по \'Python\':
         1. [example.com] Заголовок
            URL: https://...
            Контент: ..."
    """
    if not results:
        return f"По запросу '{query}' ничего не найдено"
    
    formatted = [f"Результаты поиска по '{query}':"]
    for i, item in enumerate(results):
        formatted.append(
            f"{i+1}. [{item['domain']}] {item['title']}\n"
            f"   URL: {item['url']}\n"
            f"   Обновлено: {item.get('modtime', 'N/A')}\n"
            f"   Язык: {item.get('lang', 'N/A')}\n"
            f"   Контент: {item['content']}"
        )
    return "\n\n".join(formatted)

###############################
## ПОИСК С YANDEX SEARCH API ##
###############################

class YandexSearchAPI:
    """
    Description:
    ---------------
        Асинхронный клиент для работы с Yandex Search API
    Attributes:
    ---------------
        api_key (str): API ключ от Yandex Cloud
        folder_id (str): Идентификатор каталога в Yandex Cloud
        search_type (str): Тип поиска (по умолчанию: SEARCH_TYPE_RU)
        family_mode (str): Фильтр семейного поиска (по умолчанию: FAMILY_MODE_STRICT)
        response_format (str): Формат ответа (по умолчанию: FORMAT_XML)
        base_url (str): Базовый URL для поисковых запросов
        operations_url (str): URL для проверки статуса операций
        headers (dict): Заголовки запросов с авторизацией
        logger (logging.Logger): Кастомный логгер для клиента
    """
    
    def __init__(
        self,
        api_key: str,
        folder_id: str,
        search_type: str = "SEARCH_TYPE_RU",
        family_mode: str = "FAMILY_MODE_STRICT",
        response_format: str = "FORMAT_XML",
        base_url: str = "https://searchapi.api.cloud.yandex.net",
        operations_url: str = "https://operation.api.cloud.yandex.net",
        logger: Optional[logging.Logger] = None
    ):
        self.api_key = api_key
        self.folder_id = folder_id
        self.search_type = search_type
        self.family_mode = family_mode
        self.response_format = response_format
        self.base_url = base_url
        self.operations_url = operations_url
        self.headers = {"Authorization": f"Api-Key {self.api_key}"}
        
        # Настройка логгера
        self.logger = logger or main_logger
        
        # Добавляем заглушку, если нет обработчиков
        if not self.logger.handlers:
            self.logger.addHandler(logging.NullHandler())
        
        self.logger.info("Клиент YandexSearchAPI инициализирован.")

    async def search(
        self, 
        query_text: str, 
        groups_on_page: int = 20,
        pages_to_fetch: List[int] = [0],
        docs_in_group: int = 1,
        max_passages: int = 5,
        **kwargs
    ) -> List[Dict]:
        """
        Description:
        ---------------
            Выполняет поиск по заданному запросу и возвращает результаты с нескольких страниц
            
        Args:
        ---------------
            query_text (str): Поисковый запрос
            groups_on_page (int): Количество групп результатов на странице (от 1 до 100)
            pages_to_fetch (List[int]): Список номеров страниц для получения (начинается с 0)
            docs_in_group (int): Количество документов в каждой группе результатов (от 1 до 3)
            max_passages (int): Максимальное количество текстовых фрагментов (пассажей) для каждого документа (от 1 до 5)
            **kwargs: Дополнительные параметры поиска (опционально):
                - search_type: Тип поиска (по умолчанию: SEARCH_TYPE_RU)
                    - SEARCH_TYPE_RU: Русский
                    - SEARCH_TYPE_TR: Турецкий
                    - SEARCH_TYPE_COM: Международный
                    - SEARCH_TYPE_KK: Казахский
                    - SEARCH_TYPE_BE: Белорусский
                    - SEARCH_TYPE_UZ: Узбекский
                - family_mode: Фильтрация контента (по умолчанию: FAMILY_MODE_STRICT)
                    - FAMILY_MODE_MODERATE: Умеренный фильтр
                    - FAMILY_MODE_NONE: Без фильтрации
                    - FAMILY_MODE_STRICT: Строгий семейный фильтр
                - fix_typo_mode: Исправление опечаток (по умолчанию: FIX_TYPO_MODE_ON)
                    - FIX_TYPO_MODE_ON: Автоисправление включено
                    - FIX_TYPO_MODE_OFF: Автоисправление отключено
                - sort_mode: Правило сортировки (по умолчанию: SORT_MODE_BY_RELEVANCE)
                    - SORT_MODE_BY_RELEVANCE: По релевантности
                    - SORT_MODE_BY_TIME: По времени изменения документа
                - sort_order: Порядок сортировки (по умолчанию: SORT_ORDER_DESC)
                    - SORT_ORDER_DESC: Сначала новые
                    - SORT_ORDER_ASC: Сначала старые
                - group_mode: Метод группировки (по умолчанию: GROUP_MODE_DEEP)
                    - GROUP_MODE_DEEP: Группировка по доменам
                    - GROUP_MODE_FLAT: Плоская группировка
                - region: ID региона для геотаргетинга (только для RU/TR)
                - l10n: Локализация ответа:
                    - RU: LOCALIZATION_RU, LOCALIZATION_BE, LOCALIZATION_KK, LOCALIZATION_UK
                    - TR: LOCALIZATION_TR
                    - COM: LOCALIZATION_EN
                - user_agent: Заголовок User-Agent для эмуляции устройства
            
        Returns:
        ---------------
            List[Dict]: Список словарей с распарсенными и оптимизированными результатами
            
        Workflow:
        ---------------
            1. Асинхронно отправляет запросы для каждой указанной страницы
            2. Отслеживает статус операций поиска
            3. Декодирует и парсит полученные XML-ответы
            4. Фильтрует и возвращает оптимизированные результаты
        """
        self.logger.info(f"Поиск: '{query_text}' (страницы: {pages_to_fetch})")
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                # Запускаем операции поиска
                operations = []
                for page in pages_to_fetch:
                    body = {
                        "query": {
                            "search_type": kwargs.get("search_type", self.search_type),
                            "query_text": query_text,
                            "family_mode": kwargs.get("family_mode", self.family_mode),
                            "page": page,
                            "fix_yypo_mode": kwargs.get("fix_typo_mode", "FIX_TYPO_MODE_ON")
                        },
                        "sort_spec": {
                            "sort_mode": kwargs.get("sort_mode", "SORT_MODE_BY_RELEVANCE"),
                            "sort_order": kwargs.get("sort_order", "SORT_ORDER_DESC"),
                        },
                        "group_spec": {
                            "group_mode": kwargs.get("group_mode", "GROUP_MODE_DEEP"),
                            "groups_on_page": kwargs.get("groups_on_page", groups_on_page),
                            "docs_in_group": kwargs.get("docs_in_group", docs_in_group)
                        },
                        "max_passages": kwargs.get("max_passages", max_passages),
                        "l10n": kwargs.get("l10n", "LOCALIZATION_RU"),
                        "folder_id": self.folder_id,
                        "response_format": kwargs.get("response_format", self.response_format),
                    }
                    if (kwargs.get("region")):
                        body["region"] = kwargs.get("region")
                    if (kwargs.get("user_agent")):
                        body["user_agent"] = kwargs.get("user_agent")
                    async with session.post(
                        f"{self.base_url}/v2/web/searchAsync", 
                        json=body
                    ) as resp:
                        if resp.status != 200:
                            error = await resp.text()
                            self.logger.error(f"Ошибка запроса: {resp.status} - {error}")
                            continue
                        operations.append((await resp.json())["id"])
                
                # Получаем результаты операций
                xml_results = []
                for op_id in operations:
                    attempts = 0
                    while attempts < 10:
                        async with session.get(
                            f"{self.operations_url}/operations/{op_id}"
                        ) as resp:
                            if resp.status != 200:
                                await asyncio.sleep(1)
                                attempts += 1
                                continue
                            
                            operation = await resp.json()
                            if operation.get("done"):
                                xml_results.append(
                                    base64.b64decode(
                                        operation["response"]["rawData"]
                                    ).decode("utf-8")
                                )
                                break
                        await asyncio.sleep(1)
                        attempts += 1
                
                # Парсим результаты
                parsed = []
                for xml in xml_results:
                    parsed.extend(_parse_search_results(xml))
                
                return parsed
        
        except Exception as e:
            self.logger.error(f"Ошибка поиска: {str(e)}")
            return []