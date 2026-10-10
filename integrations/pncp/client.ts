export interface PNCPQueryParams {
  dataInicial: string; // YYYYMMDD
  dataFinal: string;   // YYYYMMDD
  codigoModalidadeContratacao?: number;
  uf?: string;
  pagina?: number;
  tamanhoPagina?: number;
}

export class PNCPClient {
  private baseUrl: string;

  constructor(baseUrl = process.env.PNCP_API_BASE_URL || 'https://pncp.gov.br/api/consulta') {
    this.baseUrl = baseUrl;
  }

  async getTendersByPublication(params: PNCPQueryParams) {
    const searchParams = new URLSearchParams({
      dataInicial: params.dataInicial,
      dataFinal: params.dataFinal,
      pagina: String(params.pagina || 1),
      tamanhoPagina: String(params.tamanhoPagina || 10),
    });

    if (params.codigoModalidadeContratacao) {
      searchParams.append('codigoModalidadeContratacao', String(params.codigoModalidadeContratacao));
    }
    if (params.uf) {
      searchParams.append('uf', params.uf);
    }

    const res = await fetch(`${this.baseUrl}/v1/contratacoes/publicacao?${searchParams.toString()}`, {
      headers: {
        'Accept': 'application/json',
        'User-Agent': 'LicitacoesBrasil-Next/1.0',
      },
      next: { revalidate: 300 },
    });

    if (!res.ok) {
      throw new Error(`PNCP API Error: HTTP ${res.status}`);
    }

    return res.json();
  }

  async getTenderItems(cnpj: string, ano: number, sequencial: number) {
    const res = await fetch(`${this.baseUrl}/v1/orgaos/${cnpj}/compras/${ano}/${sequencial}/itens`);
    if (!res.ok) throw new Error(`PNCP Items Error: HTTP ${res.status}`);
    return res.json();
  }

  async getTenderFiles(cnpj: string, ano: number, sequencial: number) {
    const res = await fetch(`${this.baseUrl}/v1/orgaos/${cnpj}/compras/${ano}/${sequencial}/arquivos`);
    if (!res.ok) throw new Error(`PNCP Files Error: HTTP ${res.status}`);
    return res.json();
  }
}
