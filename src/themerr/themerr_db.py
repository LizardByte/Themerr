# standard imports
import re
from threading import Lock
import time

# local imports
from common import helpers
from common import logger

log = logger.get_logger(name=__name__)

database_cache = {}
lookup_cache = {}
last_cache_update = 0

db_field_name = {
    'movies': {'themoviedb': 'id', 'imdb': 'imdb_id'},
    'movie_collections': {'themoviedb': 'id'},
    'tv_shows': {'themoviedb': 'id'},
}

lock = Lock()


def _title_key(title: str) -> str:
    """Normalize a Plex or ThemerrDB title for matching.

    Parameters
    ----------
    title : str
        Title to normalize.

    Returns
    -------
    str
        Case-insensitive title with punctuation normalized to spaces.
    """
    return ' '.join(re.findall(r'\w+', title.casefold()))


def _index_title(index: dict, title: str, item_id: int) -> None:
    """Index a title and its optional collection suffix alias.

    Parameters
    ----------
    index : dict
        Title lookup being built.
    title : str
        ThemerrDB title.
    item_id : int
        TMDB identifier.
    """
    key = _title_key(title)
    index.setdefault(key, item_id)
    if key.endswith(' collection'):
        index.setdefault(key[:-len(' collection')], item_id)


def _refresh_database(database_type: str, databases: dict) -> None:
    """Fetch and publish one complete ThemerrDB index.

    Parameters
    ----------
    database_type : str
        ThemerrDB media category.
    databases : dict
        External database names and their metadata ID fields.
    """
    pages = helpers.json_get(
        cache_time=3600,
        url=f'https://themerr.lizardbyte.dev/ThemerrDB/{database_type}/pages.json',
    )
    page_count = pages['pages']

    id_index = {db: set() for db in databases}
    lookups = {'title': {}, 'imdb': {}}

    for page in range(page_count):
        page_data = helpers.json_get(
            cache_time=3600,
            url=f'https://themerr.lizardbyte.dev/ThemerrDB/{database_type}/all_page_{page + 1}.json',
        )

        for db in databases:
            id_index[db].update(str(item[db_field_name[database_type][db]]) for item in page_data)
        for item in page_data:
            if item.get('title'):
                _index_title(lookups['title'], item['title'], int(item['id']))
            if database_type == 'movies' and item.get('imdb_id'):
                lookups['imdb'][item['imdb_id']] = int(item['id'])

    database_cache[database_type] = id_index
    lookup_cache[database_type] = lookups


def update_cache() -> None:
    """
    Update the ThemerrDB cache.

    The pages.json file is fetched for all database types, then each all_page_N.json file is fetched to form the
    complete set of available IDs.

    Attempting to update the cache while an update is already in progress will wait until the current update is
    complete.

    Updating the cache less than an hour after the last update is a no-op.
    """
    global last_cache_update

    if time.time() - last_cache_update < 3600:
        log.info('ThemerrDB index is fresh; skipping refresh')
        return

    if lock.locked():
        log.info('Waiting for another task to finish refreshing the ThemerrDB index')
    with lock:
        if time.time() - last_cache_update < 3600:
            log.info('ThemerrDB index was refreshed by another task; skipping')
            return
        log.info('Updating ThemerrDB cache')
        for database_type, databases in db_field_name.items():
            try:
                _refresh_database(database_type, databases)

                log.info(f'{database_type}: database updated')
            except Exception as e:
                log.error(f'{database_type}: Error retrieving page index from ThemerrDB: {e}')

                database_cache[database_type] = {}
                lookup_cache[database_type] = {}

        last_cache_update = time.time()


def find_id_by_title(database_type: str, title: str) -> int | None:
    """Find a ThemerrDB TMDB identifier by title.

    Parameters
    ----------
    database_type : str
        ThemerrDB database type.
    title : str
        Plex item title.

    Returns
    -------
    int or None
        TMDB identifier when the title exists in ThemerrDB.
    """
    if database_type not in lookup_cache:
        update_cache()
    return lookup_cache.get(database_type, {}).get('title', {}).get(_title_key(title))


def find_movie_id_by_imdb(imdb_id: str) -> int | None:
    """Find a ThemerrDB movie's TMDB identifier by IMDb ID.

    Parameters
    ----------
    imdb_id : str
        IMDb identifier.

    Returns
    -------
    int or None
        TMDB identifier when the movie exists in ThemerrDB.
    """
    if 'movies' not in lookup_cache:
        update_cache()
    return lookup_cache.get('movies', {}).get('imdb', {}).get(imdb_id)


def item_exists(database_type: str, database: str, id: int | str) -> bool:
    """
    Check if an item exists in the ThemerrDB.

    Parameters
    ----------
    database_type : str
        The type of database to check for the item.

    database : str
        The database to check for the item.

    id : Union[int, str]
        The ID of the item to check for.

    Returns
    -------
    py:class:`bool`
        True if the item exists in the ThemerrDB, otherwise False.

    Examples
    --------
    >>> item_exists(database_type='movies', database='themoviedb', id=1234)
    False
    """
    if database_type not in db_field_name:
        log.critical(f'"{database_type}" is not a valid database_type. Allowed values are: {db_field_name.keys()}')
        return False

    if database_type not in database_cache:
        update_cache()

    type_cache = database_cache[database_type]
    return database in type_cache and str(id) in type_cache[database]
