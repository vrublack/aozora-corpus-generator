import sys
import argparse
import textwrap
import pathlib
import concurrent.futures

import re
import unicodedata
import jaconv
import csv
from collections import defaultdict
from zipfile import ZipFile
from io import TextIOWrapper
from lxml import html
from natto import MeCab

import logging
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)


def make_jis_unicode_map(file_path):
    d = {}
    hex_to_code = dict(zip([format(i, 'x').upper() for i in range(33, 33+95)],
                           ['{0:0>2}'.format(i) for i in range(1, 95)]))

    with open(file_path) as f:
        for line in f:
            if line[0] == '#':
                continue

            jis_field, unicode_field = line.split('\t')[0:2]

            jis_standard, jis_code = jis_field.split('-')
            if jis_standard == '3':
                men = 1
            elif jis_standard == '4':
                men = 2

            ku = hex_to_code[jis_code[0:2]]
            ten = hex_to_code[jis_code[2:4]]

            unicode_point = unicode_field.replace('U+', '')
            if unicode_point == '':  # No mapping exists.
                continue
            elif len(unicode_point) > 6:  # 2 characters
                first, second = unicode_point.split('+')
                unicode_char = chr(int(first, 16)) + chr(int(second, 16))
            else:
                unicode_char = chr(int(unicode_point, 16))

            jis_string = '{}-{}-{}'.format(men, ku, ten)

            d[jis_string] = unicode_char
    return d


def normalize_japanese_text(s):
    '''
    Normalizes to NFKC Unicode norm and converts all half-width
    alphanumerics and symbols to full-width. The latter step is needed
    for correct use with the UniDic dictionary used with MeCab.
    '''
    return jaconv.h2z(unicodedata.normalize('NFKC', s),
                      kana=False,
                      digit=True,
                      ascii=True).replace('.', '．').replace(',', '，')


def split_sentence_ja(s):
    '''
    Splits Japanese sentence on common sentence delimiters. Some care
    is taken to prevent false positives, but more sophisticated
    (non-regex) logic would be required to implement a more robust
    solution.
    '''
    delimiters = r'[!\?。．！？…]'
    closing_quotations = r'[\)）」』】］〕〉》\]]'
    return [s.strip() for s in re.sub(r'({}+)(?!{})'.format(delimiters, closing_quotations),
                                      r'\1\n',
                                      s).splitlines()]


def text_to_tokens(text):
    '''
    Returns a sequence of sentences, each comprising a sequence of
    tokens. Must be subsumed into non-lazy collection.
    '''
    unidic_features = ['pos1', 'pos2', 'pos3', 'pos4', 'cType',
                       'cForm', 'lForm', 'lemma', 'orth', 'pron', 'orthBase',
                       'pronBase', 'goshu', 'iType', 'iForm', 'fType', 'fForm']

    with MeCab() as mecab:
        for sentence in split_sentence_ja(text):
            tokens = []
            for node in mecab.parse(sentence, as_nodes=True):
                if not node.is_eos():
                    token = dict(zip(unidic_features, node.feature.split(',')))
                    if len(token) == 6:  # UNK
                        token['orth'] = node.surface
                        token['orthBase'] = node.surface
                        token['lemma'] = node.surface
                        tokens.append(token)
                    else:
                        tokens.append(token)
            yield tokens


def wakati(text):
    '''
    Returns a sequence of sentences comprised of whitespace separated tokens.
    '''
    for sentence in text_to_tokens(text):
        yield ' '.join(token['orth'] for token in sentence)


def tokenize(text, features):
    '''
    Returns a sequence of sentences comprised of whitespace separated
    tokens. Supports encoding tokens with other POS or morphological
    annotations.
    '''
    for sentence in text_to_tokens(text):
        yield ' '.join('/'.join(token[feature] for feature in features)
                       for token in sentence)


def romanize(s):
    return jaconv.kana2alphabet(jaconv.kata2hira(s.replace('・', '_')))


def read_aozora_bunko_list(path):
    ''''''
    d = defaultdict(dict)
    url_rx = re.compile(r'http://www\.aozora\.gr\.jp/cards/(\d+)/(.+)')
    with ZipFile(path) as z:
        with z.open('list_person_all_extended_utf8.csv', 'r') as f:
            for row in csv.DictReader(TextIOWrapper(f)):
                # Some works have versions in both new- and old-style
                # kana. As we are only interested in the new-style
                # version, we skip the old one while keeping only
                # old-style works.
                if row['文字遣い種別'] == '旧字旧仮名':
                    continue

                try:
                    year, month, day = re.match(r'(\d{4})（.+）年\s?(\d{1,2})月(\d{1,2})日', row['底本初版発行年1']).groups()
                except Exception:
                    year = ''

                author_ja = row['姓'] + row['名']
                author_en = row['名ローマ字'] + ' ' + row['姓ローマ字']
                title = row['作品名']
                title_ja = title
                title_en = jaconv.kana2alphabet(jaconv.kata2hira(row['作品名読み'])).title()
                subtitle = row['副題']
                if subtitle != '':
                    title_ja += ': ' + subtitle
                    title_en += ': ' + romanize(row['副題読み']).title()

                try:
                    match = url_rx.match(row['XHTML/HTMLファイルURL'])
                    id = match.group(1)
                    file_path = match.group(2)
                except AttributeError:
                    log.debug('Missing XHTML/HTML file for record {}, skipping...'.format(row))
                    pass

                d[author_ja][title] = {
                    'author_ja': author_ja,
                    'author': author_en,
                    'title_ja': title_ja,
                    'title': title_en,
                    'year': year,
                    'genre': row['分類番号'],
                    'file_path': 'aozorabunko/cards/{}/{}'.format(id, file_path),
                    'file_name': '{}_{}_{}'.format(  # TODO Do we need to shorthen these?
                        row['姓ローマ字'],
                        row['名ローマ字'][0:1],
                        romanize(row['作品名読み'][0:5]).title()
                    )
                }
    return d


def read_author_title_list(aozora_db, path):
    ''''''
    corpus_files = []
    db = []
    with open(path, newline='') as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                row['author'] = re.sub(r'\s', '', row['author'])
                match = aozora_db[row['author']][row['title']]
                corpus_files.append((match['file_name'], match['file_path']))
                db.append(row)
            except KeyError:
                log.warn('{} not in Aozora Bunko DB. Skipping...'.format(row))
    return corpus_files, db


def read_aozora_bunko_xml(path, gaiji_tr):
    ''''''
    doc = html.parse(path)
    body = doc.xpath(".//div[@class='main_text']")[0]
    if len(body) == 0:
        print('WARN: look into this file')
        body = doc.xpath(".//div")[0]

    # Remove ruby and notes:
    for e in body.xpath(".//span[@class='notes'] | .//rp | .//rt"):
        e.drop_tree()

    #<img src="../../../gaiji/1-14/1-14-45.png" alt="※(「にんべん＋爾」、第3水準1-14-45)" class="gaiji" />
    for gaiji_el in body.xpath(".//img[@class='gaiji']"):
        menkuten = re.match(r'.+gaiji/\d+-\d+/(\d-\d+-\d+)\.png', gaiji_el.get('src')).groups(1)[0]
        gaiji_el.text = gaiji_tr[menkuten]
        log.debug('Replacing JIS X {} with Unicode \'{}\''.format(menkuten, gaiji_tr[menkuten]))

    text = re.sub(r'[\r\n]+', '\n', ''.join(body.itertext()).strip(), flags=re.MULTILINE)

    return [list(wakati(paragraph)) for paragraph in text.splitlines()]


def write_corpus_file(paragraphs, file_name, prefix):
    ''''''
    with open('{}/Tokenized/{}.txt'.format(prefix, file_name), 'w') as f_tokenized:
        with open('{}/Plain/{}.txt'.format(prefix, file_name), 'w') as f_plain:
            for paragraph in paragraphs:
                f_tokenized.write('<PGB>\n'.join(re.sub(r'\s+', '\n', sentence) + '\n<EOS>\n' for sentence in paragraph))
                f_plain.write('\n'.join(paragraph) + '\n\n')


def convert_corpus_file(file_name, file_path, prefix, gaiji_tr):
    write_corpus_file(read_aozora_bunko_xml(file_path, gaiji_tr), file_name, prefix)
    return file_name, file_path, prefix


def write_metadata_file(files, metadata, aozora_db, prefix):
    metadata_fn = '{}/groups.csv'.format(prefix)
    with open(metadata_fn, 'w', newline='') as f:
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(['textid',
                         'language',
                         'corpus',
                         'author_ja',
                         'title_ja',
                         'author',
                         'title',
                         'year',
                         'genre',
                         'brow'])
        for (file_name, _), d in zip(files, metadata):
            m = aozora_db[d['author']][d['title']]
            writer.writerow([file_name + '.txt',
                             'ja',
                             'Aozora Bunko',
                             m['author_ja'],
                             m['title_ja'],
                             m['author'],
                             m['title'],
                             m['year'],
                             m['genre'],
                             d['brow']])
        log.info('Wrote metadata to {}'.format(metadata_fn))


def parse_args():
    '''Parses and returns program arguments as dictionary.'''
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent('''aozora-corpus-generator extracts given author and book pairs from Aozora Bunko and formats them into (optionally tokenized) plain text files.'''),
        epilog=textwrap.dedent('''Example usage:
python aozora-corpus-generator.py --features 'orth' --author-title-csv 'author-title.csv' --out 'Corpora/Japanese' --parallel''')
    )
    parser.add_argument('--features',
                        nargs='+',
                        help='specify which features should be extracted from morphemes (default is \'orth\')',
                        default=['orth'],
                        required=False)
    parser.add_argument('--author-title-csv',
                        nargs='+',
                        help='one or more UTF-8 formatted CSV input file(s) (default is \'author-title.csv\')',
                        default=['author-title.csv'],
                        required=True)
    parser.add_argument('--aozora-bunko-repository',
                        help='path to the aozorabunko git repository',
                        default='aozorabunko/index_pages/list_person_all_extended_utf8.zip',
                        required=False)
    parser.add_argument('--out',
                        help='output (plain, tokenized) files into given output directory',
                        default='Corpora',
                        required=True)
    parser.add_argument('--parallel',
                        help='specify if processing should be done in parallel (default=True)',
                        action='store_true',
                        default=False,
                        required=False)
    parser.add_argument('--verbose',
                        help='turns on verbose logging (default=False)',
                        action='store_true',
                        default=True,
                        required=False)

    return vars(parser.parse_args())


if __name__ == '__main__':
    args = parse_args()

    if args['verbose']:
        log.setLevel(logging.DEBUG)
        stdout_handler.setLevel(logging.DEBUG)

    log.addHandler(stdout_handler)

    gaiji_tr = make_jis_unicode_map('jisx0213-2004-std.txt')

    pathlib.Path(args['out'] + '/Tokenized').mkdir(parents=True, exist_ok=True)
    pathlib.Path(args['out'] + '/Plain').mkdir(parents=True, exist_ok=True)

    aozora_db = read_aozora_bunko_list(args['aozora_bunko_repository'])

    files, metadata = [], []
    for csv_path in args['author_title_csv']:
        fs, db = read_author_title_list(aozora_db, csv_path)
        files.extend(fs)
        metadata.extend(db)

    write_metadata_file(files, metadata, aozora_db, args['out'])

    if args['parallel']:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            futures = [executor.submit(convert_corpus_file, file_name, file_path, args['out'], gaiji_tr)
                       for (file_name, file_path) in files]
            for future in concurrent.futures.as_completed(futures):
                try:
                    file_name, file_path, prefix = future.result()
                    log.info('{} => {}/{{ Tokenized, Plain }}/{}.txt'.format(file_path, prefix, file_name))
                except Exception:
                    log.error('Process {} failed: {}'.format(future, future.result()))
    else:
        for file_name, file_path in files:
            convert_corpus_file(file_name, file_path, args['out'], gaiji_tr)
            log.info('{} => {}/{{ Tokenized, Plain }}/{}.txt'.format(file_path, args['out'], file_name))
