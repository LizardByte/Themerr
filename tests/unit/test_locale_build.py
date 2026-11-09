"""Locale compilation must work before the application has compiled translations."""

# standard imports
import gettext
import os
from pathlib import Path
import shutil
import subprocess
import sys

# lib imports
import polib


def build_tree(tmp_path):
    """Copy build inputs and fail if the application package is initialized."""
    root = Path(__file__).resolve().parents[2]
    (tmp_path / 'scripts').mkdir()
    common = tmp_path / 'src' / 'common'
    common.mkdir(parents=True)
    shutil.copyfile(root / 'scripts' / 'localize.py', tmp_path / 'scripts' / 'localize.py')
    shutil.copyfile(root / 'src' / 'common' / 'definitions.py', common / 'definitions.py')
    (common / '__init__.py').write_text("raise RuntimeError('Application initialized during locale compilation')\n")
    return tmp_path / 'scripts' / 'localize.py'


def test_locale_help_needs_no_application_or_dependencies(tmp_path):
    script = build_tree(tmp_path)
    result = subprocess.run([sys.executable, '-S', str(script), '--help'], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert '--compile' in result.stdout


def test_locale_compile_works_without_existing_translations(tmp_path):
    script = build_tree(tmp_path)
    messages = tmp_path / 'locale' / 'en' / 'LC_MESSAGES'
    messages.mkdir(parents=True)
    (messages / 'themerr.po').write_text(
        'msgid ""\nmsgstr ""\n"Language: en\\n"\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        'msgid "Theme"\nmsgstr "A theme"\n', encoding='utf-8',
    )
    environment = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}
    result = subprocess.run([sys.executable, str(script), '--compile'], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    with (messages / 'themerr.mo').open('rb') as catalog:
        assert gettext.GNUTranslations(catalog).gettext('Theme') == 'A theme'


def test_locale_extraction_cleans_template_and_preserves_crowdin_catalogs(tmp_path):
    script = build_tree(tmp_path)
    root = Path(__file__).resolve().parents[2]
    shutil.copyfile(root / 'scripts' / 'babel.cfg', tmp_path / 'scripts' / 'babel.cfg')
    (tmp_path / 'locale').mkdir()
    (tmp_path / 'web').mkdir()
    metadata_message = (
        'POT-Creation-Date: This is a translated message, not metadata. '
        'It must not be removed from the catalog.'
    )
    (tmp_path / 'src' / 'messages.py').write_text(
        f"_('Café')\n_({metadata_message!r})\n", encoding='utf-8',
    )
    translation_path = tmp_path / 'locale' / 'pt_BR' / 'LC_MESSAGES' / 'themerr.po'
    translation_path.parent.mkdir(parents=True)
    translation = polib.POFile()
    translation.header = 'Translated by Example Translator'
    translation.metadata = {
        'Language': 'pt_BR',
        'Content-Type': 'text/plain; charset=utf-8',
        'Last-Translator': 'Example Translator',
        'POT-Creation-Date': '2024-03-15 14:05+0000',
        'PO-Revision-Date': '2024-03-15 21:02+0000',
    }
    translation.append(polib.POEntry(msgid='Café', msgstr='Café traduzido'))
    translation.save(str(translation_path))
    translation_bytes = translation_path.read_bytes()
    environment = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}

    def run_locale(*arguments):
        result = subprocess.run([sys.executable, str(script), *arguments], env=environment,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr

    run_locale('--extract')
    template = tmp_path / 'locale' / 'themerr.po'
    catalog = polib.pofile(str(template))
    assert 'POT-Creation-Date' not in catalog.metadata
    assert 'PO-Revision-Date' not in catalog.metadata
    assert 'FIRST AUTHOR <EMAIL@ADDRESS>' not in catalog.header
    assert 'Last-Translator' not in catalog.metadata
    assert catalog.metadata['Report-Msgid-Bugs-To'] == 'https://github.com/Themerr/Themerr/issues'
    assert 'charset=utf-8' in catalog.metadata['Content-Type']
    assert catalog.find('Café') is not None
    assert catalog.find(metadata_message) is not None
    template_bytes = template.read_bytes()
    run_locale('--extract')
    assert template.read_bytes() == template_bytes
    assert translation_path.read_bytes() == translation_bytes
    assert set((tmp_path / 'locale').rglob('*.po')) == {template, translation_path}
