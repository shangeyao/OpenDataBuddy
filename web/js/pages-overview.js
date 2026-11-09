// 工作空间概览
import { api, fmt } from './api.js';
import { kpi, card, dataTable, statusTag, loading, emptyState } from './ui.js';

export async function renderOverview(page, ctx) {
  page.innerHTML = loading();
  const data = await api.get('/overview');
  const s = data.stats;
  const usage = await api.get('/usage').catch(() => null);

  const llmStrip = data.llm_ready
    ? `<div class="info-strip"><span class="tag blue">模型已接入</span>Agent 可执行多步工具调用；写操作会先经护栏判定。</div>`
    : `<div class="info-strip"><span class="tag orange">未接入模型</span>
        当前使用内置规则引擎（问数、扫描、建任务可用），<a href="#/platform">去平台设置接入 DeepSeek</a> 解锁开放问答。</div>`;

  const runRows = data.recent_runs || [];
  const tableRows = (data.tables || []).map((t) => ({
    ...t,
    desc: t.description || '-',
  }));

  page.innerHTML = `
    <div class="col" style="gap:16px">
      ${llmStrip}

      <div class="stat-row">
        ${kpi('数据表', fmt.num(s.tables), `共 ${fmt.num(s.rows)} 行数据`)}
        ${kpi('注册指标', fmt.num(s.metrics), '统一语义层口径')}
        ${kpi('工作流', fmt.num(s.flows), `${s.enabled_flows} 个已开启调度`)}
        ${kpi('敏感字段', fmt.num(s.sensitive_columns), 'L3 / L4 分级')}
        ${kpi('质量通过率', s.quality_pass_rate == null ? '-' : fmt.pct(s.quality_pass_rate), '最近一次执行结果')}
        ${kpi('计算资源', usage ? usage.compute.clusters : '-', usage ? `${usage.compute.cu_total} CU 配额` : '')}
        ${kpi('Agent 调用', fmt.num(data.trace?.calls || 0), data.trace?.success_rate == null ? '暂无轨迹' : `成功率 ${fmt.pct(data.trace.success_rate)}`)}
        ${kpi('磁盘占用', usage ? fmt.bytes(usage.storage.bytes) : '-', '本地数据目录')}
      </div>

      <div class="grid-2">
        ${card('最近工作流运行', runRows.length
          ? dataTable({
              rows: runRows,
              columns: [
                { title: '工作流', render: (r) => `<span class="cell-main">${r.flow_name}</span>` },
                { title: '状态', render: (r) => statusTag(r.status) },
                { title: '触发方式', render: (r) => ({ schedule: '定时调度', agent: 'Agent', manual: '手动' }[r.trigger] || r.trigger) },
                { title: '开始时间', render: (r) => `<span class="dim">${fmt.dt(r.started_at)}</span>` },
              ],
            })
          : emptyState('还没有运行记录', '到「工作流编排」点一次运行试试', '⇉'),
          { actions: '<a class="btn text sm" href="#/ops">运维监控 →</a>', flush: true })}

        ${card('工具调用分布', data.trace?.tools?.length
          ? dataTable({
              rows: data.trace.tools,
              columns: [
                { title: '工具', render: (r) => `<span class="mono" style="font-size:12.5px">${r.tool}</span>` },
                { title: '调用次数', align: 'right', render: (r) => fmt.num(r.calls) },
                { title: '成功率', align: 'right', render: (r) => fmt.pct(r.success_rate) },
                { title: '平均耗时', align: 'right', render: (r) => `${r.avg_ms} ms` },
              ],
            })
          : emptyState('还没有工具调用轨迹', '和 Buddy 聊两句就会产生', '✦'),
          { sub: 'Harness 对每次工具调用留痕', flush: true })}
      </div>

      <div class="grid-2">
        ${card('主要数据表', tableRows.length
          ? dataTable({
              rows: tableRows,
              columns: [
                { title: '表', render: (r) => `<div class="cell-main mono" style="font-size:12.5px">${r.ref}</div><div class="cell-sub">${r.desc}</div>` },
                { title: '行数', align: 'right', width: '100px', render: (r) => fmt.num(r.row_count) },
                { title: '操作', width: '70px', render: (r) => `<a class="btn-link" href="#/catalog">查看</a>` },
              ],
            })
          : emptyState('还没有数据表', '到「数据接入」上传一份 CSV', '▤'),
          { actions: '<a class="btn text sm" href="#/catalog">数据目录 →</a>', flush: true })}

        ${card('快捷入口', `<div class="col" style="gap:8px">
          ${[
            ['#/buddy', '问一个数', '用自然语言查指标并出图'],
            ['#/ingest', '接入数据', 'CSV / Excel / Parquet 自动建表'],
            ['#/semantic', '注册指标', '固化口径，避免同问两数'],
            ['#/security', '安全防护', '查看三要素拦截与注入检测'],
          ].map(([href, t, d]) => `<a class="list-item" href="${href}" style="text-decoration:none;color:inherit">
            <div class="meta"><strong>${t}</strong><span>${d}</span></div><span class="dim">›</span></a>`).join('')}
        </div>`) }
      </div>
    </div>`;
}
