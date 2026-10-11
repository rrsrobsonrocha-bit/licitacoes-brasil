import { PrismaClient } from '@prisma/client';

const prisma = new PrismaClient();

async function main() {
  console.log('Populando banco de dados com sementes oficiais...');

  // Nenhuma conta padrão/de demonstração é criada (ambiente de produção).

  // Criar Fontes Oficiais
  const pncp = await prisma.source.upsert({
    where: { id: 'pncp-source-01' },
    update: {},
    create: {
      id: 'pncp-source-01',
      name: 'Portal Nacional de Contratações Públicas (PNCP)',
      baseUrl: 'https://pncp.gov.br/api/consulta',
      officialDomain: 'pncp.gov.br',
      sourceType: 'FEDERAL',
      integrationType: 'API_OFICIAL',
      active: true,
      authorizationNotes: 'API Pública aberta instituída pela Lei nº 14.133/2021',
      lastSyncStatus: 'SUCESSO',
    },
  });

  const comprasGov = await prisma.source.upsert({
    where: { id: 'comprasgov-source-02' },
    update: {},
    create: {
      id: 'comprasgov-source-02',
      name: 'Compras.gov.br (Comprasnet)',
      baseUrl: 'https://compras.dados.gov.br',
      officialDomain: 'gov.br',
      sourceType: 'FEDERAL',
      integrationType: 'API_OFICIAL',
      active: true,
      authorizationNotes: 'Dados abertos oficiais do Governo Federal',
    },
  });

  console.log('Seed inicial executado com sucesso.');
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(async () => {
    await prisma.$disconnect();
  });
