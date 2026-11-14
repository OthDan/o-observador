# O Observador

Criado com a função de monitorar os serviços da internet localmente, com visual de jornal clássico.
Roda direto no seu computador, sem necessidade de criar conta e sem enviar seus dados para ninguém. É um programa simples, mas diferente do Downdetector: em vez de depender de relatos de usuários, ele consulta os próprios sites e as páginas oficiais de status dos serviços.

Acompanha 138 serviços em 14 categorias (bancos e pagamentos, Big Techs, marketplaces,
redes sociais, produtividade, infraestrutura de internet, streaming, jogos, inteligência
artificial, governo, logística, segurança, hospedagem e IoT).

## De onde vêm as informações

Cada serviço mostra três fontes, sempre separadas:

- **Informação oficial:** o que a própria empresa publica na sua página de status. É lida
  automaticamente quando a página usa o formato Statuspage; nos demais casos, o app só
  indica o endereço.
- **Verificação independente:** teste feito pelo seu computador a cada 60 segundos. Mostra se o
  servidor responde, não se todas as funções do serviço funcionam. Uma falha deixa o serviço
  "instável"; três seguidas o tornam "indisponível".
- **Relatos de usuários:** o app não coleta relatos, então não há dados dessa fonte.

O histórico de incidentes começa quando o app começa a observar.

## Como rodar

Requer Python 3.12 ou 3.13.

rode o comando abaixo dentro do cmd na pasta do arquivo.

```
pip install -r requirements.txt
python app.py
```

O navegador abre em `http://127.0.0.1:8765`.

## Como gerar o executável (Windows)

Deixe `app.py`, `catalogo.yaml`, `config.yaml` e `gerar_exe.bat` na mesma pasta e dê dois cliques
no `gerar_exe.bat`. O `O-Observador.exe` vai ser gerado dentro da pasta; 
Em macOS ou Linux, use `python build.py`.

## Configuração
config.yaml: de quanto em quanto tempo verificar, quantas falhas seguidas até avisar, tempo máximo de resposta (acima dele o serviço fica “instável”) e a porta.
- `catalogo.yaml`: carga inicial dos serviços. Só acrescenta serviços ausentes e nunca
  sobrescreve o que você editou no app.
- Cadastro e edição de serviços, componentes e pausa do monitoramento são feitos pela própria
  página do app.

## Observações

- Domínios e páginas de status do catálogo devem ser revisados; se uma leitura oficial falhar,
  a página do serviço avisa e o endereço pode ser corrigido na edição.
- São cerca de 140 requisições por minuto com as configurações padrão.
