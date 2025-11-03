# --- GuardiãoBot PRO v4.1 - Correção Asyncio/Thread ---
# Desenvolvido para a Rainha Ingrid
# Funcionalidades:
# 1. Conexão com banco de dados externo (Neon) via psycopg2
# 2. Mini-Cadastro, Assinaturas, Renovação, Remoção
# 3. Servidor Web (Flask) para o "Web Service" gratuito do Render
# 4. Jobs Agendados (Aniversário, Abandono, Renovação)
# 5. CORREÇÃO: Adiciona event loop de asyncio para o thread do bot

import logging
import psycopg2 
import os
from datetime import datetime, timedelta
import threading
import asyncio # <-- NOVO IMPORT para o "relógio" do bot
from flask import Flask, request

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --- CONFIGURAÇÕES PRINCIPAIS (OBRIGATÓRIO NO RENDER) ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
GRUPO_ID = int(os.environ.get("GRUPO_ID")) 
PIX_INFO = os.environ.get("PIX_INFO", "PIX: seu-email@pix.com\nValor: R$ 50,00")
PIX_RENOVACAO = os.environ.get("PIX_RENOVACAO", "PIX: seu-email@pix.com\nValor Renovação: R$ 40,00")
DIAS_ASSINATURA = int(os.environ.get("DIAS_ASSINATURA", 30))
DIAS_AVISO_RENOVACAO = int(os.environ.get("DIAS_AVISO_RENOVACAO", 3))

DATABASE_URL = os.environ.get("DATABASE_URL")
# -----------------------------------------------

# Configuração de logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- ESTADOS DA CONVERSA DE CADASTRO ---
(EMAIL, ANIVERSARIO, CONFIRMACAO) = range(3)
(PENDENTE_PAGAMENTO,) = range(3, 4) 

# --- FUNÇÕES DO BANCO DE DADOS (PostgreSQL / Neon) ---
# (Todo o código do banco de dados está correto e permanece o mesmo)

def get_db_connection():
    """Estabelece uma nova conexão com o banco de dados Neon."""
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def inicializar_db():
    """Cria a tabela de usuários se ela não existir."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id BIGINT PRIMARY KEY,
            telegram_username TEXT,
            email TEXT,
            data_aniversario DATE,
            status TEXT,
            data_cadastro TIMESTAMP,
            data_expiracao TIMESTAMP
        )
        """)
        conn.commit()
        cursor.close()
        logger.info("Banco de dados inicializado com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao inicializar DB: {e}")
    finally:
        if conn:
            conn.close()

def get_user_data(user_id):
    """Puxa todos os dados de um usuário."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM usuarios WHERE user_id = %s", (user_id,))
        colnames = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        cursor.close()
        if row:
            return dict(zip(colnames, row))
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar usuário {user_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_user_data(user_id, telegram_username, email, data_aniversario):
    """Insere ou atualiza os dados do mini-cadastro."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        data_hoje = datetime.now()
        cursor.execute("""
        INSERT INTO usuarios (user_id, telegram_username, email, data_aniversario, status, data_cadastro, data_expiracao)
        VALUES (%s, %s, %s, %s, 'pendente_pagamento', %s, NULL)
        ON CONFLICT (user_id) DO UPDATE SET
            telegram_username = EXCLUDED.telegram_username,
            email = EXCLUDED.email,
            data_aniversario = EXCLUDED.data_aniversario,
            status = 'pendente_pagamento',
            data_expiracao = NULL
        """, (user_id, telegram_username, email, data_aniversario, data_hoje))
        conn.commit()
        cursor.close()
    except Exception as e:
        logger.error(f"Erro ao atualizar dados de {user_id}: {e}")
    finally:
        if conn:
            conn.close()

def update_user_status(user_id, novo_status):
    """Atualiza apenas o status do usuário."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE usuarios SET status = %s WHERE user_id = %s", (novo_status, user_id))
        conn.commit()
        cursor.close()
    except Exception as e:
        logger.error(f"Erro ao atualizar status de {user_id}: {e}")
    finally:
        if conn:
            conn.close()

def update_user_expiry(user_id, nova_data_expiracao):
    """Atualiza a data de expiração e o status de um usuário."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE usuarios SET status = 'membro_ativo', data_expiracao = %s WHERE user_id = %s", 
            (nova_data_expiracao, user_id)
        )
        conn.commit()
        cursor.close()
    except Exception as e:
        logger.error(f"Erro ao atualizar expiração de {user_id}: {e}")
    finally:
        if conn:
            conn.close()

def get_users_para_aviso_renovacao():
    """Busca usuários ativos cuja expiração está próxima."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        data_aviso = datetime.now() + timedelta(days=DIAS_AVISO_RENOVACAO)
        cursor.execute(
            "SELECT user_id, data_expiracao FROM usuarios WHERE status = 'membro_ativo' AND data_expiracao <= %s",
            (data_aviso,)
        )
        usuarios = cursor.fetchall()
        cursor.close()
        return usuarios
    except Exception as e:
        logger.error(f"Erro ao buscar usuários para aviso: {e}")
        return []
    finally:
        if conn:
            conn.close()

def get_users_expirados():
    """Busca usuários ativos cuja data de expiração já passou."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        agora = datetime.now()
        cursor.execute(
            "SELECT user_id, telegram_username FROM usuarios WHERE status = 'membro_ativo' AND data_expiracao < %s",
            (agora,)
        )
        usuarios = cursor.fetchall()
        cursor.close()
        return usuarios
    except Exception as e:
        logger.error(f"Erro ao buscar usuários expirados: {e}")
        return []
    finally:
        if conn:
            conn.close()

# --- FUNÇÕES DE AGENDAMENTO (JOBS) ---
# (Todo o código dos jobs está correto e permanece o mesmo)

async def job_abandono_carrinho(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.user_id
    user_data = get_user_data(user_id)
    if user_data and user_data['status'] == 'pendente_pagamento':
        logger.info(f"[Job] Enviando follow-up de abandono para {user_id}")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "Servo, notei que você solicitou acesso há 24 horas, mas ainda não enviou seu tributo.\n\n"
                    "A Rainha não tolera indecisão. Se deseja mesmo servi-la, envie seu comprovante.\n\n"
                    f"Lembre-se:\n{PIX_INFO}"
                )
            )
        except Exception as e:
            logger.error(f"Erro ao enviar follow-up para {user_id}: {e}")

async def job_checa_aniversarios(context: ContextTypes.DEFAULT_TYPE):
    logger.info("[Job] Verificando aniversariantes do dia...")
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, telegram_username 
            FROM usuarios 
            WHERE EXTRACT(MONTH FROM data_aniversario) = EXTRACT(MONTH FROM CURRENT_DATE)
            AND EXTRACT(DAY FROM data_aniversario) = EXTRACT(DAY FROM CURRENT_DATE)
        """)
        aniversariantes = cursor.fetchall()
        cursor.close()
    except Exception as e:
        logger.error(f"Erro ao buscar aniversariantes: {e}")
        aniversariantes = []
    finally:
        if conn:
            conn.close()

    if not aniversariantes:
        logger.info("[Job] Nenhum aniversariante hoje.")
        return

    for user_id, username in aniversariantes:
        logger.info(f"[Job] Enviando parabéns para {username} ({user_id})")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"Olá, {username}.\n\n"
                    "Notei que hoje é seu dia especial. A Rainha Ingrid valoriza a devoção dos seus servos mais leais.\n\n"
                    "Como um presente, use o cupom `NIVER10` para obter 10% de desconto em qualquer conteúdo avulso hoje.\n\n"
                    "Feliz aniversário."
                )
            )
        except Exception as e:
            logger.error(f"Erro ao enviar parabéns para {user_id}: {e}")

async def job_aviso_renovacao(context: ContextTypes.DEFAULT_TYPE):
    logger.info("[Job] Verificando assinaturas perto de expirar...")
    usuarios = get_users_para_aviso_renovacao()
    for user_id, data_expiracao in usuarios:
        dias_restantes = (data_expiracao - datetime.now()).days
        if dias_restantes == DIAS_AVISO_RENOVACAO:
            logger.info(f"[Job] Enviando aviso de renovação para {user_id} (expira em {dias_restantes} dias)")
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"ATENÇÃO: Sua assinatura do Círculo Interno da Rainha Ingrid expira em {dias_restantes} dias.\n\n"
                        "Para garantir seu acesso e não ser removido, sua devoção deve ser renovada.\n\n"
                        "Use o comando /renovar para iniciar o processo."
                    )
                )
            except Exception as e:
                logger.error(f"Erro ao enviar aviso de renovação para {user_id}: {e}")

async def job_remove_expirados(context: ContextTypes.DEFAULT_TYPE):
    logger.info("[Job] Verificando membros expirados para remoção...")
    usuarios = get_users_expirados()
    if not usuarios:
        logger.info("[Job] Nenhum membro expirado encontrado.")
        return
    for user_id, username in usuarios:
        logger.warning(f"[Job] Removendo usuário expirado: {username} ({user_id})")
        try:
            await context.bot.ban_chat_member(chat_id=GRUPO_ID, user_id=user_id)
            await context.bot.unban_chat_member(chat_id=GRUPO_ID, user_id=user_id)
            update_user_status(user_id, 'expirado')
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "Sua assinatura do Círculo Interno expirou.\n\n"
                    "Você foi removido automaticamente do grupo. Para retornar, sua devoção deve ser provada novamente.\n\n"
                    "Use o comando /renovar para pagar sua renovação e solicitar o acesso."
                )
            )
        except Exception as e:
            logger.error(f"Erro ao remover {user_id}: {e}")
            update_user_status(user_id, 'expirado')

# --- FLUXO: INÍCIO E CADASTRO (/start, /acesso) ---
# (Todo o código de /start, /acesso, cadastro e handlers está correto e permanece o mesmo)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_data = get_user_data(user_id)
    status = user_data['status'] if user_data else None

    if status == 'membro_ativo':
        data_expira = user_data['data_expiracao'].strftime('%d/%m/%Y')
        await update.message.reply_text(f"Sua assinatura já está ativa e é válida até {data_expira}.\nUse /renovar se desejar estender seu acesso.")
        return ConversationHandler.END
    
    if status in ['pendente_aprovacao_novo', 'pendente_aprovacao_renovacao']:
        await update.message.reply_text("Seu comprovante já foi enviado e está em análise pela Rainha. Por favor, aguarde.")
        return ConversationHandler.END

    if status == 'pendente_pagamento':
        await update.message.reply_text(
            "Você já iniciou seu cadastro. Só falta enviar seu tributo.\n\n"
            f"{PIX_INFO}\n\n"
            "Envie o COMPROVANTE (foto) aqui neste chat para análise.\n\n"
            "Se desejar recomeçar seu cadastro, envie /cancelar."
        )
        return PENDENTE_PAGAMENTO
    
    if status == 'pendente_renovacao':
        await update.message.reply_text(
            "Você solicitou a renovação. Só falta enviar seu tributo.\n\n"
            f"{PIX_RENOVACAO}\n\n"
            "Envie o COMPROVANTE (foto) aqui neste chat para análise.\n\n"
            "Se desejar cancelar, envie /cancelar."
        )
        return ConversationHandler.END

    if status == 'expirado':
        await update.message.reply_text(
            "Sua assinatura expirou. Para retornar, você deve renovar.\n\n"
            "Use o comando /renovar para iniciar o processo."
        )
        return ConversationHandler.END

    logger.info(f"Iniciando novo cadastro para {user.username} ({user_id})")
    context.user_data['telegram_username'] = user.username or f"user_{user_id}"
    
    await update.message.reply_text(
        "Bem-vindo, futuro servo.\n\n"
        "Eu sou o Guardião da Rainha Ingrid e vou processar seu acesso. "
        "Para provar sua seriedade, preciso de alguns dados.\n\n"
        "Primeiro, qual o seu **E-MAIL**?"
    )
    return EMAIL

async def recebe_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = update.message.text
    if '@' not in email or '.' not in email:
        await update.message.reply_text("Isso não parece um e-mail válido. Tente novamente.")
        return EMAIL

    context.user_data['email'] = email
    logger.info(f"Recebido E-mail: {email} de {update.effective_user.id}")
    
    await update.message.reply_text(
        "E-mail registrado.\n\n"
        "Agora, informe sua **DATA DE ANIVERSÁRIO** (para futuras promoções).\n\n"
        "Use o formato `DD/MM/AAAA`."
    )
    return ANIVERSARIO

async def recebe_aniversario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data_texto = update.message.text
    try:
        data_obj = datetime.strptime(data_texto, "%d/%m/%Y")
        data_db = data_obj.date() 
        context.user_data['data_aniversario'] = data_db
        logger.info(f"Recebida Data de Aniversário: {data_db} de {update.effective_user.id}")
    except ValueError:
        await update.message.reply_text(
            "Formato de data inválido. Por favor, use exatamente `DD/MM/AAAA`.\n"
            "Exemplo: 25/12/1990"
        )
        return ANIVERSARIO

    teclado_confirmacao = [["Sim, dados corretos"], ["Não, quero recomeçar"]]
    await update.message.reply_text(
        "Ótimo. Por favor, confirme seus dados:\n\n"
        f"Usuário: @{context.user_data['telegram_username']}\n"
        f"E-mail: {context.user_data['email']}\n"
        f"Aniversário: {data_texto}\n\n"
        "Os dados estão corretos?",
        reply_markup=ReplyKeyboardMarkup(teclado_confirmacao, one_time_keyboard=True, resize_keyboard=True),
    )
    return CONFIRMACAO

async def confirmacao_dados(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "Não, quero recomeçar":
        await update.message.reply_text("Ok, vamos recomeçar do zero.", reply_markup=ReplyKeyboardRemove())
        return await start(update, context) 

    if update.message.text != "Sim, dados corretos":
        await update.message.reply_text("Por favor, use os botões 'Sim' ou 'Não'.")
        return CONFIRMACAO

    user_id = update.effective_user.id
    ud = context.user_data
    update_user_data(user_id, ud['telegram_username'], ud['email'], ud['data_aniversario'])
    
    context.job_queue.run_once(
        job_abandono_carrinho,
        when=timedelta(hours=24), 
        user_id=user_id,
        name=f"abandono_{user_id}"
    )

    logger.info(f"Cadastro finalizado para {user_id}. Status: pendente_pagamento.")
    await update.message.reply_text(
        "Cadastro salvo! Você está a um passo de servir.\n\n"
        "Seu status agora é: **PENDENTE DE PAGAMENTO**.\n\n"
        "Para finalizar, faça seu tributo de acesso:\n"
        f"----------------------\n**{PIX_INFO}**\n----------------------\n\n"
        "Após pagar, envie o **COMPROVANTE (FOTO/PRINT)** aqui mesmo neste chat.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PENDENTE_PAGAMENTO

async def recebe_comprovante_novo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    logger.info(f"Recebido comprovante NOVO de {user.username} ({user_id}). Encaminhando para Admin.")
    
    update_user_status(user_id, 'pendente_aprovacao_novo')

    botoes_admin = [
        [
            InlineKeyboardButton("✅ Aprovar NOVO", callback_data=f"aprovar_novo_{user_id}"),
            InlineKeyboardButton("❌ Recusar NOVO", callback_data=f"recusar_novo_{user_id}"),
        ]
    ]
    
    await envia_para_admin(context, user, "Rainha, novo pedido de ACESSO:", botoes_admin, update.message)
    
    await update.message.reply_text(
        "Comprovante de NOVO MEMBRO recebido.\n\n"
        "Seu pedido foi enviado diretamente à Rainha Ingrid para análise manual. "
        "Por favor, aguarde pacientemente."
    )
    return ConversationHandler.END

async def recebe_comprovante_renovacao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_data = get_user_data(user_id)
    
    if not user_data or user_data['status'] not in ['pendente_renovacao', 'expirado']:
        return

    logger.info(f"Recebido comprovante de RENOVAÇÃO de {user.username} ({user_id}). Encaminhando para Admin.")
    
    update_user_status(user_id, 'pendente_aprovacao_renovacao')

    botoes_admin = [
        [
            InlineKeyboardButton("✅ Aprovar RENOVAÇÃO", callback_data=f"aprovar_renovacao_{user_id}"),
            InlineKeyboardButton("❌ Recusar RENOVAÇÃO", callback_data=f"recusar_renovacao_{user_id}"),
        ]
    ]

    await envia_para_admin(context, user, "Rainha, novo pedido de RENOVAÇÃO:", botoes_admin, update.message)
    
    await update.message.reply_text(
        "Comprovante de RENOVAÇÃO recebido.\n\n"
        "Seu pedido foi enviado diretamente à Rainha Ingrid para análise manual. "
        "Por favor, aguarde pacientemente."
    )

async def envia_para_admin(context, user, texto_cabecalho, botoes, message_comprovante):
    try:
        user_data = get_user_data(user.id)
        detalhes_usuario = f"Usuário: @{user.username} (ID: {user.id})\nEmail: {user_data.get('email', 'N/A')}"
        
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"{texto_cabecalho}\n\n{detalhes_usuario}\nAguardando sua aprovação."
        )
        await message_comprovante.forward(chat_id=ADMIN_ID)
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text="Selecione uma ação:",
            reply_markup=InlineKeyboardMarkup(botoes)
        )
    except Exception as e:
        logger.error(f"Falha ao enviar comprovante de {user.id} para o Admin: {e}")

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text(text="Ação não permitida.")
        return

    try:
        acao, tipo, user_id_str = query.data.split("_")
        user_id = int(user_id_str)
    except ValueError:
        logger.error(f"Callback mal formatado recebido: {query.data}")
        await query.edit_message_text(text="ERRO: Callback mal formatado.")
        return

    if acao == "aprovar":
        if tipo == "novo":
            logger.info(f"Admin aprovou NOVO MEMBRO {user_id}")
            
            data_expiracao = datetime.now() + timedelta(days=DIAS_ASSINATURA)
            update_user_expiry(user_id, data_expiracao)
            
            jobs = context.job_queue.get_jobs_by_name(f"abandono_{user_id}")
            for job in jobs: job.schedule_removal()
            
            try:
                link = await context.bot.create_chat_invite_link(
                    chat_id=GRUPO_ID,
                    member_limit=1,
                    expire_date=datetime.now() + timedelta(days=1)
                )
                await context.bot.send_message(
                    chat_id=user_id,
                    text="Sua devoção foi aceita.\n\n"
                         "A Rainha Ingrid permitiu sua entrada no Círculo Interno. "
                         f"Sua assinatura é válida por {DIAS_ASSINATURA} dias.\n\n"
                         "Seu link de acesso é pessoal, intransferível e expira em 24 horas.\n\n"
                         f"{link.invite_link}\n\n"
                         "Leia as regras fixadas ao entrar."
                )
                await query.edit_message_text(text=f"✅ Acesso de NOVO MEMBRO {user_id} APROVADO.")
            
            except Exception as e:
                logger.error(f"Erro ao criar link ou enviar para {user_id}: {e}")
                await query.edit_message_text(text=f"ERRO ao aprovar {user_id}. Verifique os logs.")

        elif tipo == "renovacao":
            logger.info(f"Admin aprovou RENOVAÇÃO para {user_id}")
            
            user_data = get_user_data(user_id)
            data_expira_atual = user_data['data_expiracao'] if user_data else None
            
            base_data = datetime.now() 
            if data_expira_atual:
                base_data = max(datetime.now(), data_expira_atual)
                
            nova_data_expiracao = base_data + timedelta(days=DIAS_ASSINATURA)
            update_user_expiry(user_id, nova_data_expiracao)
            
            await context.bot.send_message(
                chat_id=user_id,
                text="Sua renovação foi confirmada.\n\n"
                     "A Rainha agradece sua lealdade contínua. "
                     f"Sua assinatura foi estendida e agora é válida até {nova_data_expiracao.strftime('%d/%m/%Y')}."
            )
            await query.edit_message_text(text=f"✅ RENOVAÇÃO de {user_id} APROVADA.")

    elif acao == "recusar":
        if tipo == "novo":
            logger.info(f"Admin recusou NOVO MEMBRO {user_id}")
            update_user_status(user_id, 'recusado')
            await context.bot.send_message(
                chat_id=user_id,
                text="Seu pedido de acesso foi analisado pela Rainha e RECUSADO.\n\n"
                     "Seu comprovante não foi aceito. Se acredita que foi um erro, inicie o processo novamente com /acesso."
            )
            await query.edit_message_text(text=f"❌ Acesso de NOVO MEMBRO {user_id} RECUSADO.")
        
        elif tipo == "renovacao":
            logger.info(f"Admin recusou RENOVAÇÃO para {user_id}")
            
            user_data = get_user_data(user_id)
            status_anterior = 'expirado' 
            if user_data and user_data['data_expiracao']:
                if user_data['data_expiracao'] > datetime.now():
                    status_anterior = 'membro_ativo'
            
            update_user_status(user_id, status_anterior)
            
            await context.bot.send_message(
                chat_id=user_id,
                text="Seu pedido de RENOVAÇÃO foi RECUSADO.\n\n"
                     "Seu comprovante não foi aceito. Seu status anterior foi mantido. "
                     "Se sua assinatura ainda estiver ativa, você permanece no grupo até a data de expiração."
            )
            await query.edit_message_text(text=f"❌ RENOVAÇÃO de {user_id} RECUSADA.")

async def renovar_acesso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    status = user_data['status'] if user_data else None

    if status not in ['membro_ativo', 'expirado', 'pendente_renovacao']:
        await update.message.reply_text("Você precisa ser um membro (ativo ou expirado) para renovar. Use /acesso para iniciar seu cadastro.")
        return

    update_user_status(user_id, 'pendente_renovacao')
    logger.info(f"Usuário {user_id} iniciou fluxo de renovação.")
    
    await update.message.reply_text(
        "Você solicitou a renovação da sua assinatura.\n\n"
        "Para provar sua lealdade contínua, envie seu tributo de renovação:\n"
        f"----------------------\n**{PIX_RENOVACAO}**\n----------------------\n\n"
        "Após pagar, envie o **COMPROVANTE (FOTO/PRINT)** aqui mesmo neste chat."
    )

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    context.user_data.clear()
    
    jobs = context.job_queue.get_jobs_by_name(f"abandono_{user_id}")
    for job in jobs:
        job.schedule_removal()

    user_data = get_user_data(user_id)
    if user_data and user_data['status'] == 'pendente_pagamento':
         update_user_status(user_id, 'recusado')

    logger.info(f"Usuário {user_id} cancelou o cadastro.")
    await update.message.reply_text(
        "Processo cancelado. Você pode recomeçar a qualquer momento usando /acesso.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END

# --- FUNÇÃO PRINCIPAL (MAIN) DO BOT ---

def run_bot() -> None:
    """Função que inicia o bot."""
    
    # --- CORREÇÃO ASYNCIO ---
    # Cria um novo event loop para este thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # -------------------------
    
    inicializar_db()
    
    application = Application.builder().token(TOKEN).build()

    job_queue = application.job_queue
    job_queue.run_daily(job_checa_aniversarios, time=datetime.strptime("09:00", "%H:%M").time())
    job_queue.run_daily(job_aviso_renovacao, time=datetime.strptime("10:00", "%H:%M").time())
    job_queue.run_daily(job_remove_expirados, time=datetime.strptime("01:00", "%H:%M").time())
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("acesso", start)],
        states={
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, recebe_email)],
            ANIVERSARIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recebe_aniversario)],
            CONFIRMACAO: [MessageHandler(filters.Regex("^(Sim, dados corretos|Não, quero recomeçar)$"), confirmacao_dados)],
            PENDENTE_PAGAMENTO: [MessageHandler(filters.PHOTO & (~filters.UpdateType.EDITED_MESSAGE), recebe_comprovante_novo)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        conversation_timeout=timedelta(hours=1)
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(admin_callback_handler))
    application.add_handler(CommandHandler("renovar", renovar_acesso))
    application.add_handler(MessageHandler(filters.PHOTO & (~filters.UpdateType.EDITED_MESSAGE) & (~filters.ChatType.GROUP), recebe_comprovante_renovacao))

    logger.info("Bot v4.1 (Neon DB + Async Fix) iniciado com sucesso.")
    application.run_polling() # Esta linha agora funcionará

# --- SERVIDOR WEB (FLASK) ---

web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    """Esta é a página que o UptimeRobot vai visitar."""
    return "O Guardião da Rainha está de pé e vigiando.", 200

def run_web_server():
    """Inicia o servidor web do Flask."""
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Iniciando servidor web na porta {port}")
    web_app.run(host='0.0.0.0', port=port)


if __name__ == "__main__":
    
    # Verificações de segurança
    if not TOKEN or not ADMIN_ID or not GRUPO_ID or not DATABASE_URL:
        raise ValueError("ERRO CRÍTICO: TELEGRAM_TOKEN, ADMIN_ID, GRUPO_ID, e DATABASE_URL são obrigatórios nas Variáveis de Ambiente!")
    
    # Inicia o Bot (função main) em um processo separado (Thread)
    logger.info("Iniciando o Bot do Telegram em um thread...")
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    
    # Inicia o Servidor Web (Flask) no processo principal
    logger.info("Iniciando o Servidor Web (Flask) no thread principal...")
    run_web_server()
