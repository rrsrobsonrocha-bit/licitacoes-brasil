'use client';

import React, { useState, useEffect } from 'react';
import { 
  Bell, 
  Send, 
  CheckCircle2, 
  AlertTriangle, 
  XCircle, 
  Server, 
  Filter, 
  RotateCw, 
  Eye, 
  Copy, 
  X, 
  Plus, 
  Layers 
} from 'lucide-react';

interface WebhookLog {
  id: string;
  rule_id?: string;
  tender_id?: string;
  target_url: string;
  canal: string;
  status_code?: number;
  tentativa_atual: number;
  sucesso: boolean;
  tempo_resposta_ms: number;
  payload_enviado?: any;
  resposta_servidor?: string;
  criado_em: string;
}

interface HealthMetrics {
  status: string;
  uptime_seconds: number;
  uptime_human: string;
  database: string;
  disparos_ultimas_24h: number;
  disparos_sucesso_24h: number;
  disparos_falha_24h: number;
  taxa_sucesso_pct: number;
  timestamp: string;
}

export default function AlertsAndWebhooksPage() {
  const [activeTab, setActiveTab] = useState<'alerts' | 'webhooks'>('webhooks');
  const [logs, setLogs] = useState<WebhookLog[]>([]);
  const [metrics, setMetrics] = useState<HealthMetrics | null>(null);
  const [loading, setLoading] = useState(false);
  const [totalCount, setTotalCount] = useState(0);
  const [page, setPage] = useState(1);

  // Filtros
  const [filterStatus, setFilterStatus] = useState('');
  const [filterCanal, setFilterCanal] = useState('');
  const [filterData, setFilterData] = useState('');
  const [filterCertame, setFilterCertame] = useState('');

  // Modal de Inspeção
  const [inspectModalOpen, setInspectModalOpen] = useState(false);
  const [selectedLog, setSelectedLog] = useState<WebhookLog | null>(null);
  const [inspectTab, setInspectTab] = useState<'payload' | 'response'>('payload');

  // Feedback de Reexecução em Linha
  const [retryingIds, setRetryingIds] = useState<Record<string, 'loading' | 'success' | 'error'>>({});
  const [syncLoading, setSyncLoading] = useState(false);

  const handleSyncOpportunities = async () => {
    setSyncLoading(true);
    try {
      const res = await fetch('/api/tenders/sync', { method: 'POST' });
      if (res.ok) {
        fetchMetrics();
        fetchLogs(page);
      }
    } catch (err) {
      console.error('Falha ao sincronizar:', err);
    } finally {
      setSyncLoading(false);
    }
  };

  useEffect(() => {
    if (activeTab === 'webhooks') {
      fetchMetrics();
      fetchLogs(1);
    }
  }, [activeTab]);

  const fetchMetrics = async () => {
    try {
      const res = await fetch('/api/health/metrics');
      if (res.ok) {
        const data = await res.json();
        setMetrics(data);
      }
    } catch (err) {
      console.error('Falha ao carregar métricas:', err);
    }
  };

  const fetchLogs = async (pageNum = 1) => {
    setLoading(true);
    setPage(pageNum);
    try {
      const params = new URLSearchParams({
        page: pageNum.toString(),
        page_size: '15'
      });
      if (filterStatus) params.append('status', filterStatus);
      if (filterCanal) params.append('canal', filterCanal);
      if (filterData) params.append('data', filterData);
      if (filterCertame) params.append('certame_id', filterCertame.trim());

      const res = await fetch(`/api/alerts/webhooks/logs?${params.toString()}`);
      if (res.ok) {
        const data = await res.json();
        setLogs(data.items || []);
        setTotalCount(data.total || 0);
      }
    } catch (err) {
      console.error('Falha ao obter logs:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleRetry = async (logId: string) => {
    setRetryingIds(prev => ({ ...prev, [logId]: 'loading' }));
    try {
      const res = await fetch(`/api/alerts/webhooks/${logId}/retry`, {
        method: 'POST'
      });
      if (res.ok) {
        setRetryingIds(prev => ({ ...prev, [logId]: 'success' }));
        setTimeout(() => {
          fetchMetrics();
          fetchLogs(page);
        }, 1200);
      } else {
        setRetryingIds(prev => ({ ...prev, [logId]: 'error' }));
      }
    } catch {
      setRetryingIds(prev => ({ ...prev, [logId]: 'error' }));
    }
  };

  const openInspector = (log: WebhookLog) => {
    setSelectedLog(log);
    setInspectTab('payload');
    setInspectModalOpen(true);
  };

  const copyText = (txt: string) => {
    navigator.clipboard.writeText(txt);
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      {/* Cabeçalho */}
      <div className="bg-slate-900 text-white py-8 border-b border-slate-800">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div>
              <h1 className="text-2xl font-bold flex items-center gap-2">
                <Bell className="w-6 h-6 text-sky-400" /> Radar de Oportunidades & Auditoria de Webhooks
              </h1>
              <p className="text-xs text-slate-400 mt-1">
                Acompanhe alertas automatizados e audite a latência e entregas para n8n, Evolution API e Z-API.
              </p>
            </div>
            <div className="flex gap-2">
              <button 
                onClick={handleSyncOpportunities} 
                disabled={syncLoading}
                className="bg-emerald-600 hover:bg-emerald-500 text-white font-semibold px-4 py-2 rounded-xl text-xs flex items-center gap-2 transition disabled:opacity-50"
              >
                <RotateCw className={`w-4 h-4 ${syncLoading ? 'animate-spin' : ''}`} />
                {syncLoading ? 'Sincronizando...' : 'Atualizar Oportunidades'}
              </button>
              <button 
                onClick={() => setActiveTab('alerts')} 
                className="bg-sky-600 hover:bg-sky-500 text-white font-semibold px-4 py-2 rounded-xl text-xs flex items-center gap-2 transition"
              >
                <Plus className="w-4 h-4" /> Novo Alerta
              </button>
            </div>
          </div>

          {/* Abas */}
          <div className="flex gap-4 mt-6 border-b border-slate-800 -mb-8">
            <button
              onClick={() => setActiveTab('alerts')}
              className={`px-4 py-2.5 text-xs font-bold border-b-2 flex items-center gap-2 transition ${
                activeTab === 'alerts' 
                  ? 'border-sky-400 text-sky-400' 
                  : 'border-transparent text-slate-400 hover:text-slate-200'
              }`}
            >
              <Bell className="w-4 h-4" /> Meus Radares
            </button>
            <button
              onClick={() => setActiveTab('webhooks')}
              className={`px-4 py-2.5 text-xs font-bold border-b-2 flex items-center gap-2 transition ${
                activeTab === 'webhooks' 
                  ? 'border-sky-400 text-sky-400' 
                  : 'border-transparent text-slate-400 hover:text-slate-200'
              }`}
            >
              <Send className="w-4 h-4" /> Histórico e Auditoria de Disparos
            </button>
          </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {activeTab === 'webhooks' ? (
          <div className="space-y-6">
            {/* Cards de Métricas */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
              <div className="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex items-center justify-between">
                <div>
                  <span className="text-[11px] font-semibold text-slate-500 block">Disparos (24h)</span>
                  <span className="text-xl font-black text-slate-900 mt-1 block">
                    {metrics ? metrics.disparos_ultimas_24h : '--'}
                  </span>
                </div>
                <div className="w-10 h-10 rounded-xl bg-sky-50 text-sky-600 flex items-center justify-center">
                  <Send className="w-5 h-5" />
                </div>
              </div>

              <div className="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex items-center justify-between">
                <div>
                  <span className="text-[11px] font-semibold text-slate-500 block">Taxa de Sucesso</span>
                  <span className="text-xl font-black text-emerald-600 mt-1 block">
                    {metrics ? `${metrics.taxa_sucesso_pct}%` : '--%'}
                  </span>
                </div>
                <div className="w-10 h-10 rounded-xl bg-emerald-50 text-emerald-600 flex items-center justify-center">
                  <CheckCircle2 className="w-5 h-5" />
                </div>
              </div>

              <div className="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex items-center justify-between">
                <div>
                  <span className="text-[11px] font-semibold text-slate-500 block">Disparos com Falha</span>
                  <span className="text-xl font-black text-rose-600 mt-1 block">
                    {metrics ? metrics.disparos_falha_24h : '--'}
                  </span>
                </div>
                <div className="w-10 h-10 rounded-xl bg-rose-50 text-rose-600 flex items-center justify-center">
                  <AlertTriangle className="w-5 h-5" />
                </div>
              </div>

              <div className="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex items-center justify-between">
                <div>
                  <span className="text-[11px] font-semibold text-slate-500 block">Uptime & Saúde Banco</span>
                  <span className="text-xs font-bold text-slate-700 mt-1 block">
                    {metrics ? `${metrics.uptime_human} (${metrics.database})` : '--'}
                  </span>
                </div>
                <div className="w-10 h-10 rounded-xl bg-indigo-50 text-indigo-600 flex items-center justify-center">
                  <Server className="w-5 h-5" />
                </div>
              </div>
            </div>

            {/* Barra de Filtros */}
            <div className="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex flex-wrap items-center justify-between gap-3 text-xs">
              <div className="flex flex-wrap items-center gap-3">
                <div>
                  <label className="block text-[10px] font-bold text-slate-500 mb-0.5">STATUS</label>
                  <select 
                    value={filterStatus} 
                    onChange={e => setFilterStatus(e.target.value)} 
                    className="p-2 border border-slate-200 rounded-xl bg-white"
                  >
                    <option value="">Todos os Status</option>
                    <option value="SUCESSO">Apenas Sucesso</option>
                    <option value="FALHA">Apenas Falha</option>
                  </select>
                </div>

                <div>
                  <label className="block text-[10px] font-bold text-slate-500 mb-0.5">CANAL</label>
                  <select 
                    value={filterCanal} 
                    onChange={e => setFilterCanal(e.target.value)} 
                    className="p-2 border border-slate-200 rounded-xl bg-white"
                  >
                    <option value="">Todos os Canais</option>
                    <option value="N8N">n8n</option>
                    <option value="EVOLUTION_API">Evolution API</option>
                    <option value="ZAPI">Z-API</option>
                    <option value="GENERIC">Genérico</option>
                  </select>
                </div>

                <div>
                  <label className="block text-[10px] font-bold text-slate-500 mb-0.5">DATA</label>
                  <input 
                    type="date" 
                    value={filterData} 
                    onChange={e => setFilterData(e.target.value)} 
                    className="p-2 border border-slate-200 rounded-xl bg-white"
                  />
                </div>

                <div>
                  <label className="block text-[10px] font-bold text-slate-500 mb-0.5">ID CERTAME</label>
                  <input 
                    type="text" 
                    placeholder="Ex: id da licitação" 
                    value={filterCertame} 
                    onChange={e => setFilterCertame(e.target.value)} 
                    className="p-2 border border-slate-200 rounded-xl bg-white w-48"
                  />
                </div>
              </div>

              <div className="flex items-center gap-2 mt-auto">
                <button 
                  onClick={() => fetchLogs(1)} 
                  className="bg-sky-600 hover:bg-sky-500 text-white font-semibold px-4 py-2 rounded-xl flex items-center gap-1.5 transition"
                >
                  <Filter className="w-3.5 h-3.5" /> Filtrar
                </button>
                <button 
                  onClick={() => {
                    setFilterStatus('');
                    setFilterCanal('');
                    setFilterData('');
                    setFilterCertame('');
                    fetchLogs(1);
                  }} 
                  className="bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold px-3 py-2 rounded-xl flex items-center gap-1.5 transition"
                >
                  <RotateCw className="w-3.5 h-3.5" /> Limpar
                </button>
              </div>
            </div>

            {/* Tabela de Disparos */}
            <div className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="bg-slate-50 border-b border-slate-200 text-slate-600 font-bold">
                    <tr>
                      <th className="p-3.5">Canal</th>
                      <th className="p-3.5">URL Alvo</th>
                      <th className="p-3.5">Status & Código</th>
                      <th className="p-3.5">Latência</th>
                      <th className="p-3.5">Tentativa</th>
                      <th className="p-3.5">Data / Hora</th>
                      <th className="p-3.5 text-right">Ações de Auditoria</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 text-slate-700">
                    {loading ? (
                      <tr>
                        <td colSpan={7} className="p-8 text-center text-slate-400">
                          Carregando auditoria de disparos...
                        </td>
                      </tr>
                    ) : logs.length === 0 ? (
                      <tr>
                        <td colSpan={7} className="p-8 text-center text-slate-400">
                          Nenhum disparo encontrado para os filtros selecionados.
                        </td>
                      </tr>
                    ) : (
                      logs.map(log => {
                        // Badges Semânticos (Verde / Amarelo / Vermelho)
                        const isSuccess = log.sucesso;
                        const isWarning = !isSuccess && log.status_code && log.status_code >= 400 && log.status_code < 500;
                        const isDanger = !isSuccess && !isWarning;

                        return (
                          <tr key={log.id} className="hover:bg-slate-50 transition">
                            <td className="p-3.5">
                              <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-100 border border-slate-200">
                                {log.canal}
                              </span>
                            </td>
                            <td className="p-3.5 max-w-xs truncate font-mono text-[11px]" title={log.target_url}>
                              {log.target_url}
                            </td>
                            <td className="p-3.5">
                              {isSuccess && (
                                <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">
                                  <CheckCircle2 className="w-3 h-3" /> SUCESSO ({log.status_code || 200})
                                </span>
                              )}
                              {isWarning && (
                                <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-bold bg-amber-50 text-amber-700 border border-amber-200">
                                  <AlertTriangle className="w-3 h-3" /> HTTP {log.status_code}
                                </span>
                              )}
                              {isDanger && (
                                <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-bold bg-rose-50 text-rose-700 border border-rose-200">
                                  <XCircle className="w-3 h-3" /> FALHA {log.status_code ? `(${log.status_code})` : ''}
                                </span>
                              )}
                            </td>
                            <td className="p-3.5 font-mono font-semibold text-slate-600">
                              {log.tempo_resposta_ms} ms
                            </td>
                            <td className="p-3.5">
                              <span className="px-2 py-0.5 bg-slate-100 rounded text-[10px] font-semibold text-slate-600">
                                {log.tentativa_atual}ª tentativa
                              </span>
                            </td>
                            <td className="p-3.5 text-slate-500 whitespace-nowrap text-[11px]">
                              {new Date(log.criado_em).toLocaleString('pt-BR')}
                            </td>
                            <td className="p-3.5 text-right whitespace-nowrap space-x-1">
                              <button
                                onClick={() => openInspector(log)}
                                className="px-2.5 py-1.5 rounded-lg border border-slate-200 text-slate-700 hover:bg-slate-100 font-semibold text-[11px] inline-flex items-center gap-1"
                              >
                                <Eye className="w-3 h-3" /> Inspecionar
                              </button>
                              <button
                                onClick={() => handleRetry(log.id)}
                                disabled={retryingIds[log.id] === 'loading'}
                                className="px-2.5 py-1.5 rounded-lg bg-sky-50 hover:bg-sky-100 text-sky-700 border border-sky-200 font-semibold text-[11px] inline-flex items-center gap-1 disabled:opacity-50"
                              >
                                <RotateCw className={`w-3 h-3 ${retryingIds[log.id] === 'loading' ? 'animate-spin' : ''}`} />
                                {retryingIds[log.id] === 'loading'
                                  ? 'Enviando...'
                                  : retryingIds[log.id] === 'success'
                                  ? 'Reenviado!'
                                  : 'Reexecutar'}
                              </button>
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>

              <div className="p-4 border-t border-slate-100 flex items-center justify-between text-xs text-slate-500">
                <span>Total: {totalCount} registros</span>
                <div className="flex gap-2">
                  <button
                    disabled={page <= 1}
                    onClick={() => fetchLogs(page - 1)}
                    className="px-3 py-1.5 border border-slate-200 rounded-lg disabled:opacity-40"
                  >
                    Anterior
                  </button>
                  <button
                    disabled={page * 15 >= totalCount}
                    onClick={() => fetchLogs(page + 1)}
                    className="px-3 py-1.5 border border-slate-200 rounded-lg disabled:opacity-40"
                  >
                    Próxima
                  </button>
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="bg-white rounded-2xl border border-slate-200 p-8 text-center text-slate-500 text-sm">
            <Layers className="w-12 h-12 text-slate-300 mx-auto mb-3" />
            Visualização de Alertas Ativos. Use o painel para gerenciar os radares ou a aba de Webhooks para auditar disparos.
          </div>
        )}
      </div>

      {/* Modal de Inspeção */}
      {inspectModalOpen && selectedLog && (
        <div className="fixed inset-0 bg-slate-900/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-3xl w-full p-6 shadow-2xl flex flex-col max-h-[90vh]">
            <div className="flex justify-between items-center pb-3 border-b border-slate-100">
              <div>
                <h3 className="text-sm font-bold text-slate-900 flex items-center gap-2">
                  <Eye className="w-4 h-4 text-indigo-600" /> Inspeção de Disparo de Webhook
                </h3>
                <p className="text-[11px] text-slate-500 mt-0.5">
                  ID: {selectedLog.id} • Canal: {selectedLog.canal} • {selectedLog.tempo_resposta_ms} ms
                </p>
              </div>
              <button onClick={() => setInspectModalOpen(false)} className="text-slate-400 hover:text-slate-600">
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="flex gap-2 mt-4 border-b border-slate-200 text-xs">
              <button
                onClick={() => setInspectTab('payload')}
                className={`px-3 py-2 font-bold border-b-2 ${
                  inspectTab === 'payload'
                    ? 'border-indigo-600 text-indigo-600'
                    : 'border-transparent text-slate-500 hover:text-slate-700'
                }`}
              >
                Payload Enviado (JSON)
              </button>
              <button
                onClick={() => setInspectTab('response')}
                className={`px-3 py-2 font-bold border-b-2 ${
                  inspectTab === 'response'
                    ? 'border-indigo-600 text-indigo-600'
                    : 'border-transparent text-slate-500 hover:text-slate-700'
                }`}
              >
                Resposta do Servidor
              </button>
            </div>

            <div className="mt-4 flex-1 overflow-y-auto">
              {inspectTab === 'payload' ? (
                <div>
                  <div className="flex justify-between items-center mb-1.5">
                    <span className="text-[10px] font-bold text-slate-500 uppercase">Corpo JSON:</span>
                    <button 
                      onClick={() => copyText(JSON.stringify(selectedLog.payload_enviado || {}, null, 2))}
                      className="text-[11px] text-indigo-600 font-semibold hover:underline flex items-center gap-1"
                    >
                      <Copy className="w-3 h-3" /> Copiar
                    </button>
                  </div>
                  <pre className="bg-slate-900 text-emerald-400 p-4 rounded-xl text-xs font-mono overflow-x-auto whitespace-pre-wrap max-h-96">
                    {JSON.stringify(selectedLog.payload_enviado || {}, null, 2)}
                  </pre>
                </div>
              ) : (
                <div>
                  <div className="flex justify-between items-center mb-1.5">
                    <span className="text-[10px] font-bold text-slate-500 uppercase">Retorno do Servidor:</span>
                    <button 
                      onClick={() => copyText(selectedLog.resposta_servidor || '')}
                      className="text-[11px] text-indigo-600 font-semibold hover:underline flex items-center gap-1"
                    >
                      <Copy className="w-3 h-3" /> Copiar
                    </button>
                  </div>
                  <pre className="bg-slate-900 text-slate-200 p-4 rounded-xl text-xs font-mono overflow-x-auto whitespace-pre-wrap max-h-96">
                    {selectedLog.resposta_servidor || 'Sem corpo de resposta retornado.'}
                  </pre>
                </div>
              )}
            </div>

            <div className="mt-4 pt-3 border-t border-slate-100 flex justify-end">
              <button
                onClick={() => setInspectModalOpen(false)}
                className="px-4 py-2 rounded-xl text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold"
              >
                Fechar
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
