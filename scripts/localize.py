"""
scripts/localize.py

Extract the source template and compile localization catalogs supplied by Crowdin.
"""
# standard imports
import argparse
import os
import subprocess

project_name = 'Themerr'

script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
locale_dir = os.path.join(root_dir, 'locale')


def _clean_metadata(po_path):
    with open(po_path, encoding='utf-8') as stream:
        content = stream.read()
    header, separator, messages = content.partition('\n\n')
    header = '\n'.join(
        line for line in header.split('\n')
        if not line.startswith((
            '"POT-Creation-Date:',
            '"PO-Revision-Date:',
            '# FIRST AUTHOR <EMAIL@ADDRESS>,',
            '"Last-Translator: FULL NAME <EMAIL@ADDRESS>',
        ))
    )
    with open(po_path, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(f'{header}{separator}{messages}')


def babel_extract():
    """Executes `pybabel extract` in subprocess."""
    commands = [
        'pybabel',
        'extract',
        '-F', os.path.join(script_dir, 'babel.cfg'),
        '-o', os.path.join(locale_dir, f'{project_name.lower()}.po'),
        '--sort-by-file',
        f'--msgid-bugs-address=https://github.com/Themerr/{project_name}/issues',
        f'--copyright-holder={project_name}',
        f'--project={project_name}',
        '--version=v0',
        '--add-comments=NOTE',
        './src',
        './web',
    ]

    print(commands)
    subprocess.check_output(args=commands, cwd=root_dir)
    _clean_metadata(os.path.join(locale_dir, f'{project_name.lower()}.po'))


def babel_compile():
    """Executes `pybabel compile` in subprocess."""
    commands = [
        'pybabel',
        'compile',
        '-d', locale_dir,
        '-D', project_name.lower()
    ]

    print(commands)
    subprocess.check_output(args=commands, cwd=root_dir)


if __name__ == '__main__':
    # Set up and gather command line arguments
    parser = argparse.ArgumentParser(
        description='Extract the source template or compile translations supplied by Crowdin.')

    parser.add_argument('--extract', action='store_true', help='Extract messages from python files and templates.')
    parser.add_argument('--compile', action='store_true', help='Compile translated locales.')

    args = parser.parse_args()

    if args.extract:
        babel_extract()

    if args.compile:
        babel_compile()
