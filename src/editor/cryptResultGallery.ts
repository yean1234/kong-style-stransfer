const runId = location.pathname.split('/')[2]
document.title = 'Crypt 마을 · 크리스마스 변환 결과'
const style = document.createElement('style')
style.textContent = 'body{margin:24px;background:#171b25;color:#e8edf7;font:16px system-ui}a{color:#efc781}h1{font-size:28px}.maps,.cards{display:grid;gap:16px}.maps{grid-template-columns:1fr 1fr}.cards{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}.card{background:#262d3b;padding:18px;border-radius:12px}.pair{display:flex;gap:12px;height:170px;align-items:center}.pair img{width:46%;max-height:170px;object-fit:contain;image-rendering:pixelated;background:#414653}.maps img{width:100%;image-rendering:pixelated}button{color:#17202b}small{color:#b2bed2}@media(max-width:700px){.maps{grid-template-columns:1fr}}'
document.head.append(style)
const title = document.createElement('h1')
title.textContent = 'Crypt 마을 · 크리스마스 밤'
const summary = document.createElement('p')
const paletteSwatch = document.createElement('a'); paletteSwatch.textContent = '팔레트 견본 열기'; paletteSwatch.target = '_blank'; paletteSwatch.rel = 'noopener'; paletteSwatch.hidden = true
const maps = document.createElement('section'); maps.className = 'maps'
for (const [filename, label] of [['original-map.png', '원본'], ['preview.png', '밤 + FLUX 장식 미리보기']]) {
  const card = document.createElement('div'); card.className = 'card'
  const heading = document.createElement('h2'); heading.textContent = label
  const img = new Image(); img.alt = label
  if (filename === 'original-map.png') img.src = filename
  else img.dataset.preview = 'true'
  card.append(heading, img); maps.append(card)
}
const cards = document.createElement('section'); cards.className = 'cards'
const game = document.createElement('section'); game.className = 'card'
document.body.append(title, summary, paletteSwatch, maps, document.createElement('hr'), cards, game)
const refresh = async () => {
  const [spec, status] = await Promise.all([
    fetch('sources.json', {cache:'no-store'}).then(r => r.json()),
    fetch('status.json', {cache:'no-store'}).then(r => r.json())
  ])
  title.textContent = status.mapId === 'town' ? `마을(town) · ${status.prompt || '스타일 변환'}` : status.mapId === 'floor-1-ruins' ? '1층 폐허 마을 · 겨울 변환' : 'Crypt 마을 · 크리스마스 밤'
  document.title = title.textContent
  summary.textContent = `${status.status} · ${status.completed_objects ?? 0}/${status.total_objects}종 · ${status.instances}곳 배치 · 적용 ${status.applied_objects ?? 0} · 제외 ${status.failed_objects ?? 0} · 평가 불가 ${status.unscored_objects ?? 0} · ${status.gate_mode ?? '이전 게이트'} ${status.gate_config_version ?? ''}`
  paletteSwatch.hidden = !status.palette_swatch
  if (status.palette_swatch) paletteSwatch.href = status.palette_swatch
  const revision = String(status.preview_revision ?? status.completed_objects ?? 0)
  if (status.game_preview && !game.childElementCount) {
    const heading = document.createElement('h2'); heading.textContent = '게임 적용 확인 · 브라우저 검증 화면'
    const img = new Image(); img.src = status.game_preview; img.alt = 'Crypt 마을 게임 적용'; img.style.width = '100%'
    game.append(heading, img)
  }
  if (status.preview) {
    const img = maps.querySelector<HTMLImageElement>('[data-preview]')!
    if (img.dataset.revision !== revision) { img.src = `${status.preview}?v=${revision}`; img.dataset.revision = revision }
  }
  cards.replaceChildren()
  const planAssets: Set<string> | undefined = ['crypt-ruins-plan-v1','town-plan-v1'].includes(status.pipeline)
    ? new Set((await fetch('plan.json').then(r => r.json())).map((row: {asset: string}) => row.asset))
    : undefined
  if (planAssets) summary.textContent += ' · 선택하지 않은 오브젝트는 이전 결과 유지'
  for (const variant of spec.variants) {
    if (planAssets && !planAssets.has(variant.id)) continue
    const result = status.object_results[variant.id] ?? {}
    const card = document.createElement('article'); card.className = 'card'
    const name = document.createElement('h3'); name.textContent = variant.id
    if (result.postprocess_profile) name.textContent += ' · 원본 유지 / 장식 여백 +3px'
    const pair = document.createElement('div'); pair.className = 'pair'
    const original = new Image(); original.src = `${variant.id}-original.png`; original.alt = '원본'
    pair.append(original)
    if (['applied','applied_with_warning','unscored','recorded','passed','ready'].includes(result.status)) {
      const output = new Image(); output.src = `${variant.id}/composite.png?v=${revision}`; output.alt = '원본 + FLUX 장식'; pair.append(output)
    }
    const time = document.createElement('p')
    const scores = result.scores ?? {}
    const scoreText = scores.status === 'scored' ? `AI 추정 · SC ${scores.SC?.toFixed(2) ?? '—'} · PQ ${scores.PQ?.toFixed(2) ?? '—'} · O ${scores.O?.toFixed(2) ?? '—'}` : scores.status === 'unscored' ? `AI 평가 불가${scores.reason ? ` · ${scores.reason}` : ''}` : ''
    time.textContent = [result.decision ?? result.status ?? '대기', scoreText, `재생성 ${result.retries ?? 0}회`, result.shared_request ? `공유 시트 FLUX 요청 ${Math.round(result.generation_seconds)}초 (개별 생성 시간이 아닙니다)` : result.reused_from ? '기존 FLUX 출력 재사용 · 새 생성 아님' : result.generation_seconds ? `FLUX 요청 ${Math.round(result.generation_seconds)}초` : ''].filter(Boolean).join(' · ')
    if (result.warnings?.length) time.textContent += ` · ${result.warnings.join(', ')}`
    if (result.frost_only) time.textContent += ' · 눈 장식 미검출: 서리 색 보정만 적용'
    const link = document.createElement('a'); link.href = `${result.shared_request ?? variant.id}/generation.json`; link.textContent = '생성 설정'
    card.append(name, pair, time, link); cards.append(card)
  }
  if (['running','queued'].includes(status.status)) setTimeout(() => { void refresh() }, 5000)
}
void refresh().catch(error => { summary.textContent = `결과 조회 실패: ${String(error)}` })
