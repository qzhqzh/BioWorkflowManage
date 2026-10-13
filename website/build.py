#!/usr/bin/env python3
"""Build the independent public product site with Python's standard library."""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / 'site.json').read_text())
OUT = ROOT / 'dist'
REPO = CONFIG['repository']
ORIGIN = CONFIG['origin']
PAGES = [
    ('overview', '认识底座'), ('quickstart', '快速安装'), ('deployment', '部署与运维'),
    ('usage', '第一次使用'), ('integration', '接入指南'), ('ai', '给 AI 的文档'),
    ('evaluation', '试部署与测评'), ('feedback', '问题反馈'), ('releases', '版本与兼容'),
]
ICONS = {
    'arrow': '<path d="M5 12h14m-6-6 6 6-6 6"/>',
    'external': '<path d="M8 5h11v11M19 5 5 19"/>',
    'copy': '<rect x="8" y="8" width="12" height="13" rx="1.5"/><path d="M16 8V3H4v13h4"/>',
    'file': '<path d="M6 2h8l5 5v15H6V2zm8 0v6h5M9 12h7M9 16h7"/>',
    'monitor': '<rect x="2" y="3" width="20" height="14" rx="1.5"/><path d="M12 17v4m-5 0h10"/>',
    'database': '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v13c0 4 16 4 16 0V5M4 11c0 4 16 4 16 0"/>',
    'gear': '<path d="m10 2-1 3-3 1-3-1-2 4 2 2v3l-2 2 2 4 3-1 3 1 1 3h4l1-3 3-1 3 1 2-4-2-2v-3l2-2-2-4-3 1-3-1-1-3z" transform="translate(1 -1) scale(.91)"/><circle cx="12" cy="11.5" r="3"/>',
    'tool': '<path d="M14 3a6 6 0 0 0-7 8L2 18a2.7 2.7 0 0 0 4 4l7-7a6 6 0 0 0 8-7l-4 4-4-4z"/>',
    'book': '<path d="M12 5v16M12 5C8 2 4 2 2 4v16c3-2 6-2 10 1 4-3 7-3 10-1V4c-3-2-6-2-10 1"/>',
    'flask': '<path d="M9 2h6M10 2v7L4 19c-1 2 0 3 2 3h12c2 0 3-1 2-3L14 9V2M7 15h10"/>',
}

def icon(name: str, extra: str = '') -> str:
    return f'<svg class="icon {extra}" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def header(active: str = '') -> str:
    def current(key):
        return ' aria-current="page"' if active == key else ''
    return f'''<a class="skip-link" href="#main">跳到正文</a>
<header class="site-header"><div class="wrap header-inner">
<a class="brand" href="/">BioWorkflowManage<span class="brand-version">v1.1.0</span></a>
<button class="menu-toggle" aria-expanded="false" aria-controls="site-nav">菜单</button>
<nav class="site-nav" id="site-nav" aria-label="主导航">
<a href="/"{current('product')}>产品</a><a href="/docs/overview/"{current('docs')}>文档</a>
<a href="/docs/ai/"{current('ai')}>给 AI 的文档</a><a href="{REPO}">GitHub</a></nav>
<div class="header-action"><a class="button small" href="/docs/quickstart/">开始安装 {icon('arrow')}</a></div>
</div></header>'''


def footer() -> str:
    return f'''<footer class="site-footer"><div class="wrap footer-inner"><a class="footer-name" href="/">BioWorkflowManage<span>v1.1.0</span></a><nav class="footer-links" aria-label="页脚"><a href="{REPO}">GitHub</a><a href="/docs/feedback/">问题反馈</a><a href="/docs/releases/">版本说明</a></nav></div></footer>'''


def shell(title: str, description: str, path: str, content: str, active: str = '') -> str:
    fingerprint = hashlib.sha256((ROOT / 'assets/site.css').read_bytes() + (ROOT / 'assets/site.js').read_bytes()).hexdigest()[:12]
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · BioWorkflowManage</title><meta name="description" content="{html.escape(description, quote=True)}">
<link rel="canonical" href="{ORIGIN}{path}"><meta property="og:type" content="website"><meta property="og:locale" content="zh_CN"><meta property="og:title" content="{html.escape(title, quote=True)}"><meta property="og:description" content="{html.escape(description, quote=True)}"><meta property="og:url" content="{ORIGIN}{path}"><meta name="theme-color" content="#154ad6">
<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/assets/site.css?v={fingerprint}"><link rel="alternate" type="text/plain" href="/llms.txt" title="给 AI 的文档索引"><script src="/assets/site.js?v={fingerprint}" defer></script>
</head><body>{header(active)}{content}{footer()}</body></html>'''


def code_block(code: str, ident: str, dark: bool = False, label: str = '复制代码') -> str:
    return f'''<div class="code-block{' dark' if dark else ''}"><pre><code id="{ident}">{html.escape(code.rstrip())}</code></pre><span class="code-status" role="status" aria-live="polite"></span><button class="copy-button" type="button" data-copy-target="#{ident}" data-copy-label="{label}" aria-label="{label}">{icon('copy')}</button></div>'''


def homepage() -> str:
    command = 'git clone --branch v1.1.0 --depth 1 ' + chr(92) + '\n  https://github.com/qzhqzh/BioWorkflowManage.git'
    def docs_stack(labels):
        return '<div class="document-stack">' + ''.join('<div>' + icon('file') + '<span>' + label + '</span></div>' for label in labels) + '</div>'
    content = f'''<main id="main" class="wrap">
<section class="hero" aria-labelledby="hero-title"><div><h1 id="hero-title">轻松集成，<br>即刻开跑。</h1><p class="hero-description">可复用的生信分析底座。</p></div>
<div class="hero-start"><h2>独立部署，统一执行。</h2><p class="engine-line">MiniWDL / Nextflow</p><div class="hero-actions"><a class="button" href="/docs/quickstart/">开始安装 {icon('arrow')}</a><a class="button text" href="{REPO}/tree/v1.1.0">查看源码 {icon('external')}</a></div></div>
<div class="hero-scenarios"><h2>接入场景</h2><ul class="scenario-list"><li>{icon('monitor')}Web 应用</li><li>{icon('database')}业务后台</li><li>{icon('gear')}自动化任务</li></ul></div></section>
<section class="pipeline" aria-label="执行链路：分析产品、输入预检、执行管理、结果交付">
<div class="pipeline-label"><strong>执行链路</strong><span class="pipeline-caption">BIO<br>ANALYSIS<br>PIPELINE</span></div>
<article class="stage"><span class="stage-number">01</span><h2><a href="/docs/integration/#contract">分析产品</a></h2><p>标准化分析工具与流程</p><div class="stage-art">{docs_stack(['ToolSpec','Workflow','Contract'])}</div><span class="port" aria-hidden="true"></span></article>
<article class="stage"><span class="stage-number">02</span><h2><a href="/docs/integration/#inputs">输入预检</a></h2><p>检查输入数据与参数</p><div class="stage-art"><div class="precheck"><div class="input-list"><span>样本数据</span><span>参数配置</span></div><svg class="input-wires" viewBox="0 0 40 108" preserveAspectRatio="none" aria-hidden="true"><path d="M0 25h10c12 0 10 26 23 26h7M0 83h10c12 0 10-28 23-28h7"/><path d="m33 47 7 4-7 4m0-4 7 4-7 4"/></svg><div class="validation-node">输入<br>预检</div></div></div><span class="port" aria-hidden="true"></span></article>
<article class="stage"><span class="stage-number">03</span><h2><a href="/docs/deployment/#engines">执行管理</a></h2><p>统一调度与流程执行</p><div class="stage-art"><div class="runtime"><div class="runtime-core"><strong>BioWorkflowManage</strong><small>v1.1.0</small></div><svg class="runtime-wires" viewBox="0 0 240 35" preserveAspectRatio="none" aria-hidden="true"><path d="M120 0v15H50v18M120 15h70v18m-144-5 4 5 4-5m132 0 4 5 4-5"/></svg><div class="runtime-engines"><span>MiniWDL</span><span>Nextflow</span></div></div></div><span class="port" aria-hidden="true"></span></article>
<article class="stage"><span class="stage-number">04</span><h2><a href="/docs/integration/#outputs">结果交付</a></h2><p>输出可验证的结果文件</p><div class="stage-art">{docs_stack(['运行记录','输出清单','SHA-256'])}</div><span class="port" aria-hidden="true"></span></article>
</section>
<section class="entry-panels" aria-label="获取源码与接入文档"><div class="source-entry"><h2>获取 v1.1.0</h2>{code_block(command,'source-command',True,'复制获取源码命令')}<p>Docker Compose · 源码部署</p></div><div class="ai-entry"><h2>给 AI 的文档</h2><p>接入契约与示例。</p><a class="artifact-link" href="/docs/integration.md">{icon('file')}<span>integration.md</span><span class="artifact-type">Markdown</span>{icon('external','arrow')}</a><a class="artifact-link" href="/openapi.json">{icon('file')}<span>openapi.json</span><span class="artifact-type">OpenAPI</span>{icon('external','arrow')}</a><button class="button secondary" type="button" data-copy-url="/docs/ai-context.md" data-copy-label="复制接入说明">{icon('copy')}复制接入说明</button></div></section>
<nav class="doc-strip" aria-label="开始使用"><div class="doc-strip-heading"><strong>文档</strong><small>DOCUMENTATION</small></div><a href="/docs/quickstart/">{icon('file')}快速安装 {icon('arrow','arrow')}</a><a href="/docs/deployment/">{icon('tool')}部署与运维 {icon('arrow','arrow')}</a><a href="/docs/integration/">{icon('book')}接入指南 {icon('arrow','arrow')}</a><a href="/docs/evaluation/">{icon('flask')}试部署与测评 {icon('arrow','arrow')}</a></nav>
</main>'''
    return shell(CONFIG['slogan'], CONFIG['description'], '/', content, 'product')


def inline(text: str) -> str:
    tokens = []
    def reserve(value):
        tokens.append(value)
        return '\x00' + str(len(tokens) - 1) + '\x00'
    text = re.sub(r'`([^`]+)`', lambda match: reserve('<code>' + html.escape(match[1]) + '</code>'), text)
    def link(match):
        name, url = match.groups()
        if urlparse(url).scheme not in ('', 'http', 'https'):
            raise ValueError('Unsupported link scheme: ' + url)
        return reserve('<a href="' + html.escape(url, quote=True) + '">' + html.escape(name) + '</a>')
    text = re.sub(r'\[([^\]]+)\]\(([^\s)]+)\)', link, text)
    text = html.escape(text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    return re.sub(r'\x00(\d+)\x00', lambda match: tokens[int(match[1])], text)


def render_markdown(source: str):
    lines = source.splitlines()
    title = lines.pop(0).removeprefix('# ').strip()
    while lines and not lines[0].strip():
        lines.pop(0)
    lead = lines.pop(0).strip()
    output, toc = [], []
    i = 0
    code_count, check_count = 0, 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith('```'):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                code.append(lines[i]); i += 1
            if i == len(lines):
                raise ValueError('Unclosed code fence')
            code_count += 1
            output.append(code_block('\n'.join(code), f'code-{code_count}'))
            i += 1
            continue
        heading = re.match(r'^(#{2,3}) (.+?)\s*\{#([a-z0-9-]+)\}$', line)
        if heading:
            level, label, anchor = len(heading[1]), heading[2], heading[3]
            output.append(f'<h{level} id="{anchor}">{inline(label)}</h{level}>')
            if level == 2:
                toc.append((anchor, label))
            i += 1
            continue
        if line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = [cell.strip() for cell in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r'[:\s-]+', cell) for cell in cells):
                    rows.append(cells)
                i += 1
            output.append('<div class="table-scroll"><table><thead><tr>' + ''.join('<th scope="col">' + inline(cell) + '</th>' for cell in rows[0]) + '</tr></thead><tbody>')
            for row in rows[1:]:
                output.append('<tr>' + ''.join('<td>' + inline(cell) + '</td>' for cell in row) + '</tr>')
            output.append('</tbody></table></div>')
            continue
        if line.startswith('> '):
            quote = []
            while i < len(lines) and lines[i].strip().startswith('> '):
                quote.append(lines[i].strip()[2:]); i += 1
            output.append('<blockquote><p>' + inline(' '.join(quote)) + '</p></blockquote>')
            continue
        is_check = line.startswith('- [ ] ')
        is_list = line.startswith('- ') or bool(re.match(r'^\d+\. ', line))
        if is_list:
            ordered = bool(re.match(r'^\d+\. ', line))
            tag = 'ol' if ordered else 'ul'
            output.append(f'<{tag}' + (' class="check-list"' if is_check else '') + '>')
            while i < len(lines):
                item = lines[i].strip()
                if ordered:
                    found = re.match(r'^\d+\. (.+)', item)
                    if not found:
                        break
                    output.append('<li>' + inline(found[1]) + '</li>')
                elif is_check:
                    if not item.startswith('- [ ] '):
                        break
                    check_count += 1
                    output.append(f'<li><label><input type="checkbox" id="check-{check_count}" data-evaluation-check><span>' + inline(item[6:]) + '</span></label></li>')
                else:
                    if not item.startswith('- ') or item.startswith('- [ ] '):
                        break
                    output.append('<li>' + inline(item[2:]) + '</li>')
                i += 1
            output.append(f'</{tag}>')
            continue
        paragraph = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r'^(#{1,3} |```|\| |> |- |\d+\. )', lines[i]):
            paragraph.append(lines[i].strip()); i += 1
        output.append('<p>' + inline(' '.join(paragraph)) + '</p>')
    return title, lead, '\n'.join(output), toc


def docs_page(slug: str, source: str) -> str:
    title, lead, article, toc = render_markdown(source)
    links = ''.join(f'<a href="/docs/{key}/"' + (' aria-current="page"' if key == slug else '') + f'>{label}</a>' for key, label in PAGES)
    toc_links = ''.join(f'<a href="#{anchor}">{html.escape(label)}</a>' for anchor, label in toc)
    index = [key for key, _ in PAGES].index(slug)
    previous = PAGES[index - 1] if index else None
    next_page = PAGES[index + 1] if index + 1 < len(PAGES) else None
    neighbors = ('<a href="/docs/' + previous[0] + '/">← ' + previous[1] + '</a>') if previous else '<a href="/">← 产品首页</a>'
    if next_page:
        neighbors += '<a href="/docs/' + next_page[0] + '/">' + next_page[1] + ' →</a>'
    extras = ''
    if slug == 'evaluation':
        extras = '<div class="evaluation-toolbar"><p><strong data-evaluation-count>0 项已确认</strong> · 仅记录本机自评进度</p><button class="button secondary" type="button" data-export-evaluation>下载测评记录</button><p class="status-message" data-evaluation-storage role="status"></p></div>'
    if slug == 'feedback':
        article += (ROOT / 'feedback-form.html').read_text()
    content = f'''<div class="doc-layout"><aside class="doc-sidebar"><h2>使用文档</h2><nav aria-label="文档导航">{links}</nav><p class="sidebar-meta">基础版 v1.1.0<br>文档更新：{CONFIG['updated']}</p></aside>
<main id="main" class="doc-main"><div class="breadcrumbs"><a href="/">产品</a> / 文档 / {title}</div><h1>{title}</h1><p class="doc-lead">{inline(lead)}</p><div class="doc-meta"><span>适用版本 v1.1.0</span><span>{CONFIG['updated']}</span><a href="/docs/{slug}.md">阅读 Markdown {icon('external')}</a></div>{extras}<article class="doc-article">{article}</article><nav class="doc-next" aria-label="相邻文档">{neighbors}</nav></main>
<aside class="doc-toc"><strong>本页内容</strong><nav aria-label="本页目录">{toc_links}</nav></aside></div>'''
    return shell(title, lead, '/docs/' + slug + '/', content, 'ai' if slug == 'ai' else 'docs')


def build():
    if OUT.is_symlink():
        raise RuntimeError('Build output must not be a symlink')
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    shutil.copytree(ROOT / 'assets', OUT / 'assets')
    shutil.copytree(ROOT / 'downloads', OUT / 'downloads')
    (OUT / 'index.html').write_text(homepage())
    public_docs = []
    for slug, label in PAGES:
        source = (ROOT / 'content' / (slug + '.md')).read_text()
        target = OUT / 'docs' / slug
        target.mkdir(parents=True)
        (target / 'index.html').write_text(docs_page(slug, source))
        (OUT / 'docs' / (slug + '.md')).write_text(source)
        public_docs.append((slug, label, source))
    api = subprocess.run(['git', 'show', CONFIG['release_commit'] + ':schemas/integration-openapi-v1.json'], cwd=ROOT.parent, check=True, capture_output=True).stdout
    (OUT / 'openapi.json').write_bytes(api)
    (OUT / 'docs' / 'index.html').write_text(shell('使用文档', '安装、部署与接入 BioWorkflowManage。', '/docs/', '<main id="main" class="wrap not-found"><h1>使用文档</h1><p>从固定版本开始，逐步完成部署、运行与业务接入。</p><a class="button" href="/docs/overview/">阅读文档</a></main>', 'docs'))
    context = '# BioWorkflowManage v1.1.0 接入上下文\n\n软件版本：1.1.0。API 文档修订：1.7.0。站点：https://bwm.qzhqzh.com。\n\n' + (ROOT / 'content/ai.md').read_text() + '\n\n---\n\n' + (ROOT / 'content/integration.md').read_text()
    (OUT / 'docs/ai-context.md').write_text(context)
    llms = '# BioWorkflowManage\n\n> 可复用的生信分析底座。基础版 v1.1.0。\n\n## 使用文档\n\n' + ''.join(f'- [{label}]({ORIGIN}/docs/{slug}.md)\n' for slug, label, _ in public_docs) + f'\n## 机器可读入口\n\n- [接入上下文]({ORIGIN}/docs/ai-context.md)\n- [OpenAPI 1.7.0]({ORIGIN}/openapi.json)\n- [完整公开文档]({ORIGIN}/llms-full.txt)\n- [源码 v1.1.0]({REPO}/tree/v1.1.0)\n'
    (OUT / 'llms.txt').write_text(llms)
    (OUT / 'llms-full.txt').write_text(llms + '\n\n---\n\n' + '\n\n---\n\n'.join(source for _, _, source in public_docs))
    (OUT / '404.html').write_text(shell('页面未找到', '返回产品首页或快速安装指南。', '/404.html', '<main id="main" class="wrap not-found"><p>404</p><h1>这个页面没有找到。</h1><p>可以从产品首页或快速安装指南继续。</p><a class="button" href="/">返回首页</a> <a class="button text" href="/docs/quickstart/">快速安装</a></main>'))
    (OUT / 'robots.txt').write_text('User-agent: *\nAllow: /\nSitemap: ' + ORIGIN + '/sitemap.xml\n')
    urls = ['/'] + ['/docs/' + slug + '/' for slug, _ in PAGES]
    (OUT / 'sitemap.xml').write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join('<url><loc>' + ORIGIN + path + '</loc><lastmod>' + CONFIG['updated'] + '</lastmod></url>' for path in urls) + '</urlset>\n')
    downloads = sorted(path for path in (OUT / 'downloads').iterdir() if path.is_file())
    (OUT / 'downloads/SHA256SUMS').write_text(''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n' for path in downloads))
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT.parent, check=True, capture_output=True, text=True).stdout.strip()
    manifest = dict(CONFIG, site_commit=commit, api_sha256=hashlib.sha256(api).hexdigest())
    (OUT / 'version.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(f'Built {len(PAGES) + 3} HTML pages, public Markdown, OpenAPI and downloads in {OUT}')


if __name__ == '__main__':
    build()
