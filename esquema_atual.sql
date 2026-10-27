--
-- PostgreSQL database dump
--

\restrict oSJCM1M8d6JnmYSXJsdd5M9IU8uLaRqwYgLEbng78tWITHU0xCBAR7arxyFXAtX

-- Dumped from database version 17.11 (Debian 17.11-0+deb13u1)
-- Dumped by pg_dump version 17.11 (Debian 17.11-0+deb13u1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: campanha_contatos; Type: TABLE; Schema: public; Owner: irg
--

CREATE TABLE public.campanha_contatos (
    id bigint NOT NULL,
    campanha_id integer NOT NULL,
    lote_id integer,
    lista_contato_id bigint,
    telefone text NOT NULL,
    ordem_telefone smallint DEFAULT 1 NOT NULL,
    uf text,
    cidade text,
    dados jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'pendente'::text NOT NULL,
    tentativas integer DEFAULT 0 NOT NULL,
    proxima_tentativa timestamp with time zone DEFAULT now() NOT NULL,
    ultimo_resultado text,
    atualizado_em timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT campanha_contatos_status_check CHECK ((status = ANY (ARRAY['pendente'::text, 'discando'::text, 'finalizado'::text])))
);


ALTER TABLE public.campanha_contatos OWNER TO irg;

--
-- Name: campanha_contatos_id_seq; Type: SEQUENCE; Schema: public; Owner: irg
--

CREATE SEQUENCE public.campanha_contatos_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.campanha_contatos_id_seq OWNER TO irg;

--
-- Name: campanha_contatos_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: irg
--

ALTER SEQUENCE public.campanha_contatos_id_seq OWNED BY public.campanha_contatos.id;


--
-- Name: campanhas; Type: TABLE; Schema: public; Owner: irg
--

CREATE TABLE public.campanhas (
    id integer NOT NULL,
    projeto_id integer NOT NULL,
    nome text NOT NULL,
    origem text NOT NULL,
    lista_id integer,
    filtro jsonb DEFAULT '{}'::jsonb NOT NULL,
    quarentena_dias integer,
    max_tentativas integer DEFAULT 3 NOT NULL,
    janela_inicio time without time zone DEFAULT '09:00:00'::time without time zone NOT NULL,
    janela_fim time without time zone DEFAULT '20:00:00'::time without time zone NOT NULL,
    status text DEFAULT 'rascunho'::text NOT NULL,
    criada_em timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT campanhas_check CHECK (((origem = 'base'::text) OR (lista_id IS NOT NULL))),
    CONSTRAINT campanhas_origem_check CHECK ((origem = ANY (ARRAY['base'::text, 'lista'::text]))),
    CONSTRAINT campanhas_status_check CHECK ((status = ANY (ARRAY['rascunho'::text, 'ativa'::text, 'pausada'::text, 'encerrada'::text])))
);


ALTER TABLE public.campanhas OWNER TO irg;

--
-- Name: campanhas_id_seq; Type: SEQUENCE; Schema: public; Owner: irg
--

CREATE SEQUENCE public.campanhas_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.campanhas_id_seq OWNER TO irg;

--
-- Name: campanhas_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: irg
--

ALTER SEQUENCE public.campanhas_id_seq OWNED BY public.campanhas.id;


--
-- Name: clientes; Type: TABLE; Schema: public; Owner: irg
--

CREATE TABLE public.clientes (
    id integer NOT NULL,
    nome text NOT NULL,
    criado_em timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.clientes OWNER TO irg;

--
-- Name: clientes_id_seq; Type: SEQUENCE; Schema: public; Owner: irg
--

CREATE SEQUENCE public.clientes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.clientes_id_seq OWNER TO irg;

--
-- Name: clientes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: irg
--

ALTER SEQUENCE public.clientes_id_seq OWNED BY public.clientes.id;


--
-- Name: lista_contatos; Type: TABLE; Schema: public; Owner: irg
--

CREATE TABLE public.lista_contatos (
    id bigint NOT NULL,
    lista_id integer NOT NULL,
    linha integer NOT NULL,
    uf text,
    cidade text,
    dados jsonb DEFAULT '{}'::jsonb NOT NULL,
    telefones text[] DEFAULT '{}'::text[] NOT NULL,
    problemas text[] DEFAULT '{}'::text[] NOT NULL
);


ALTER TABLE public.lista_contatos OWNER TO irg;

--
-- Name: lista_contatos_id_seq; Type: SEQUENCE; Schema: public; Owner: irg
--

CREATE SEQUENCE public.lista_contatos_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.lista_contatos_id_seq OWNER TO irg;

--
-- Name: lista_contatos_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: irg
--

ALTER SEQUENCE public.lista_contatos_id_seq OWNED BY public.lista_contatos.id;


--
-- Name: listas; Type: TABLE; Schema: public; Owner: irg
--

CREATE TABLE public.listas (
    id integer NOT NULL,
    projeto_id integer NOT NULL,
    nome text NOT NULL,
    arquivo text,
    importada_em timestamp with time zone DEFAULT now() NOT NULL,
    linhas integer,
    telefones_validos integer,
    invalidos integer,
    duplicados integer,
    bloqueados integer,
    colunas text[]
);


ALTER TABLE public.listas OWNER TO irg;

--
-- Name: listas_id_seq; Type: SEQUENCE; Schema: public; Owner: irg
--

CREATE SEQUENCE public.listas_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.listas_id_seq OWNER TO irg;

--
-- Name: listas_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: irg
--

ALTER SEQUENCE public.listas_id_seq OWNED BY public.listas.id;


--
-- Name: lotes; Type: TABLE; Schema: public; Owner: irg
--

CREATE TABLE public.lotes (
    id integer NOT NULL,
    campanha_id integer NOT NULL,
    sorteado_em timestamp with time zone DEFAULT now() NOT NULL,
    solicitados integer NOT NULL,
    inseridos integer NOT NULL,
    regras jsonb NOT NULL
);


ALTER TABLE public.lotes OWNER TO irg;

--
-- Name: lotes_id_seq; Type: SEQUENCE; Schema: public; Owner: irg
--

CREATE SEQUENCE public.lotes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.lotes_id_seq OWNER TO irg;

--
-- Name: lotes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: irg
--

ALTER SEQUENCE public.lotes_id_seq OWNED BY public.lotes.id;


--
-- Name: projetos; Type: TABLE; Schema: public; Owner: irg
--

CREATE TABLE public.projetos (
    id integer NOT NULL,
    cliente_id integer NOT NULL,
    nome text NOT NULL,
    criado_em timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.projetos OWNER TO irg;

--
-- Name: projetos_id_seq; Type: SEQUENCE; Schema: public; Owner: irg
--

CREATE SEQUENCE public.projetos_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.projetos_id_seq OWNER TO irg;

--
-- Name: projetos_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: irg
--

ALTER SEQUENCE public.projetos_id_seq OWNED BY public.projetos.id;


--
-- Name: regras_rediscagem; Type: TABLE; Schema: public; Owner: irg
--

CREATE TABLE public.regras_rediscagem (
    resultado text NOT NULL,
    finaliza boolean NOT NULL,
    intervalo interval,
    conta_tentativa boolean DEFAULT true NOT NULL
);


ALTER TABLE public.regras_rediscagem OWNER TO irg;

--
-- Name: tentativas; Type: TABLE; Schema: public; Owner: irg
--

CREATE TABLE public.tentativas (
    id bigint NOT NULL,
    campanha_id integer NOT NULL,
    campanha_contato_id bigint NOT NULL,
    telefone text NOT NULL,
    canal text,
    operador text,
    inicio timestamp with time zone DEFAULT now() NOT NULL,
    atendida_em timestamp with time zone,
    fim timestamp with time zone,
    resultado text,
    causa_codigo integer,
    causa_texto text,
    conversa_s integer,
    tabulacao text
);


ALTER TABLE public.tentativas OWNER TO irg;

--
-- Name: tentativas_id_seq; Type: SEQUENCE; Schema: public; Owner: irg
--

CREATE SEQUENCE public.tentativas_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tentativas_id_seq OWNER TO irg;

--
-- Name: tentativas_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: irg
--

ALTER SEQUENCE public.tentativas_id_seq OWNED BY public.tentativas.id;


--
-- Name: campanha_contatos id; Type: DEFAULT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanha_contatos ALTER COLUMN id SET DEFAULT nextval('public.campanha_contatos_id_seq'::regclass);


--
-- Name: campanhas id; Type: DEFAULT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanhas ALTER COLUMN id SET DEFAULT nextval('public.campanhas_id_seq'::regclass);


--
-- Name: clientes id; Type: DEFAULT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.clientes ALTER COLUMN id SET DEFAULT nextval('public.clientes_id_seq'::regclass);


--
-- Name: lista_contatos id; Type: DEFAULT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.lista_contatos ALTER COLUMN id SET DEFAULT nextval('public.lista_contatos_id_seq'::regclass);


--
-- Name: listas id; Type: DEFAULT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.listas ALTER COLUMN id SET DEFAULT nextval('public.listas_id_seq'::regclass);


--
-- Name: lotes id; Type: DEFAULT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.lotes ALTER COLUMN id SET DEFAULT nextval('public.lotes_id_seq'::regclass);


--
-- Name: projetos id; Type: DEFAULT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.projetos ALTER COLUMN id SET DEFAULT nextval('public.projetos_id_seq'::regclass);


--
-- Name: tentativas id; Type: DEFAULT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.tentativas ALTER COLUMN id SET DEFAULT nextval('public.tentativas_id_seq'::regclass);


--
-- Name: campanha_contatos campanha_contatos_campanha_id_telefone_key; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanha_contatos
    ADD CONSTRAINT campanha_contatos_campanha_id_telefone_key UNIQUE (campanha_id, telefone);


--
-- Name: campanha_contatos campanha_contatos_pkey; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanha_contatos
    ADD CONSTRAINT campanha_contatos_pkey PRIMARY KEY (id);


--
-- Name: campanhas campanhas_pkey; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanhas
    ADD CONSTRAINT campanhas_pkey PRIMARY KEY (id);


--
-- Name: clientes clientes_nome_key; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.clientes
    ADD CONSTRAINT clientes_nome_key UNIQUE (nome);


--
-- Name: clientes clientes_pkey; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.clientes
    ADD CONSTRAINT clientes_pkey PRIMARY KEY (id);


--
-- Name: lista_contatos lista_contatos_pkey; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.lista_contatos
    ADD CONSTRAINT lista_contatos_pkey PRIMARY KEY (id);


--
-- Name: listas listas_pkey; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.listas
    ADD CONSTRAINT listas_pkey PRIMARY KEY (id);


--
-- Name: lotes lotes_pkey; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.lotes
    ADD CONSTRAINT lotes_pkey PRIMARY KEY (id);


--
-- Name: projetos projetos_cliente_id_nome_key; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.projetos
    ADD CONSTRAINT projetos_cliente_id_nome_key UNIQUE (cliente_id, nome);


--
-- Name: projetos projetos_pkey; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.projetos
    ADD CONSTRAINT projetos_pkey PRIMARY KEY (id);


--
-- Name: regras_rediscagem regras_rediscagem_pkey; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.regras_rediscagem
    ADD CONSTRAINT regras_rediscagem_pkey PRIMARY KEY (resultado);


--
-- Name: tentativas tentativas_pkey; Type: CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.tentativas
    ADD CONSTRAINT tentativas_pkey PRIMARY KEY (id);


--
-- Name: ix_fila; Type: INDEX; Schema: public; Owner: irg
--

CREATE INDEX ix_fila ON public.campanha_contatos USING btree (campanha_id, status, proxima_tentativa);


--
-- Name: ix_tentativas; Type: INDEX; Schema: public; Owner: irg
--

CREATE INDEX ix_tentativas ON public.tentativas USING btree (campanha_id, inicio);


--
-- Name: ix_tentativas_tel; Type: INDEX; Schema: public; Owner: irg
--

CREATE INDEX ix_tentativas_tel ON public.tentativas USING btree (telefone);


--
-- Name: campanha_contatos campanha_contatos_campanha_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanha_contatos
    ADD CONSTRAINT campanha_contatos_campanha_id_fkey FOREIGN KEY (campanha_id) REFERENCES public.campanhas(id);


--
-- Name: campanha_contatos campanha_contatos_lista_contato_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanha_contatos
    ADD CONSTRAINT campanha_contatos_lista_contato_id_fkey FOREIGN KEY (lista_contato_id) REFERENCES public.lista_contatos(id);


--
-- Name: campanha_contatos campanha_contatos_lote_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanha_contatos
    ADD CONSTRAINT campanha_contatos_lote_id_fkey FOREIGN KEY (lote_id) REFERENCES public.lotes(id);


--
-- Name: campanhas campanhas_lista_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanhas
    ADD CONSTRAINT campanhas_lista_id_fkey FOREIGN KEY (lista_id) REFERENCES public.listas(id);


--
-- Name: campanhas campanhas_projeto_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.campanhas
    ADD CONSTRAINT campanhas_projeto_id_fkey FOREIGN KEY (projeto_id) REFERENCES public.projetos(id);


--
-- Name: lista_contatos lista_contatos_lista_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.lista_contatos
    ADD CONSTRAINT lista_contatos_lista_id_fkey FOREIGN KEY (lista_id) REFERENCES public.listas(id) ON DELETE CASCADE;


--
-- Name: listas listas_projeto_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.listas
    ADD CONSTRAINT listas_projeto_id_fkey FOREIGN KEY (projeto_id) REFERENCES public.projetos(id);


--
-- Name: lotes lotes_campanha_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.lotes
    ADD CONSTRAINT lotes_campanha_id_fkey FOREIGN KEY (campanha_id) REFERENCES public.campanhas(id);


--
-- Name: projetos projetos_cliente_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.projetos
    ADD CONSTRAINT projetos_cliente_id_fkey FOREIGN KEY (cliente_id) REFERENCES public.clientes(id);


--
-- Name: tentativas tentativas_campanha_contato_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.tentativas
    ADD CONSTRAINT tentativas_campanha_contato_id_fkey FOREIGN KEY (campanha_contato_id) REFERENCES public.campanha_contatos(id);


--
-- Name: tentativas tentativas_campanha_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: irg
--

ALTER TABLE ONLY public.tentativas
    ADD CONSTRAINT tentativas_campanha_id_fkey FOREIGN KEY (campanha_id) REFERENCES public.campanhas(id);


--
-- PostgreSQL database dump complete
--

\unrestrict oSJCM1M8d6JnmYSXJsdd5M9IU8uLaRqwYgLEbng78tWITHU0xCBAR7arxyFXAtX

