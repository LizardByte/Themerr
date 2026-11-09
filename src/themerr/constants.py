# issue url constants
base_url = 'https://github.com/Themerr/ThemerrDB/issues/new?assignees='
issue_label = 'request-theme'
issue_template = 'theme.yml'
url_name = 'database_url'
title_prefix = {
    'movies': '[MOVIE]: ',
    'movie_collections': '[MOVIE COLLECTION]: ',
    'tv_shows': '[TV SHOW]: ',
}
url_prefix = {
    'movies': 'https://www.themoviedb.org/movie/',
    'movie_collections': 'https://www.themoviedb.org/collection/',
    'tv_shows': 'https://www.themoviedb.org/tv/',
}

# two additional strings to fill in later, item title and item url
issue_urls = {
    'movies': f'{base_url}'
              f'&labels={issue_label}'
              f'&template={issue_template}'
              f'&title={title_prefix['movies']}{'{}'}'
              f'&{url_name}={url_prefix['movies']}{'{}'}',
    'movie_collections': f'{base_url}'
                         f'&labels={issue_label}'
                         f'&template={issue_template}'
                         f'&title={title_prefix['movie_collections']}{'{}'}'
                         f'&{url_name}={url_prefix['movie_collections']}{'{}'}',
    'tv_shows': f'{base_url}'
                f'&labels={issue_label}'
                f'&template={issue_template}'
                f'&title={title_prefix['tv_shows']}{'{}'}'
                f'&{url_name}={url_prefix['tv_shows']}{'{}'}',
}
