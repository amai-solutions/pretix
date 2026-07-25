# Rama `azrahel` — branding Azrahel del Mayor sobre pretix

Fork de [pretix/pretix](https://github.com/pretix/pretix) mantenido por **AMAI Solutions** para la
plataforma de entradas de **Azrahel del Mayor** (https://entradas.azrahel.amai.run).

**Base:** tag estable `v2026.6.0`
**Despliegue:** Dokploy (`apps.amai.run`), proyecto *Azrahel*, build desde este repo con el
`Dockerfile` oficial de pretix (all-in-one: nginx + gunicorn + celery vía supervisord).

---

## Qué cambia esta rama respecto a upstream

Assets estáticos + una línea del arranque. Cero cambios en código Python, plantillas o lógica de
negocio — para que el rebase sobre versiones nuevas de pretix entre limpio.

> **Por qué se toca `pretix.bash`:** el entrypoint oficial arranca gunicorn con `2 × nproc` workers y
> celery con un proceso por núcleo. En un servidor grande y compartido (32 núcleos) eso son ~58
> procesos y **más de 11 GB de RAM** para una instancia con cinco eventos. Ahora `CELERY_CONCURRENCY`
> se puede fijar por entorno, igual que `NUM_WORKERS`. Sin valor, el comportamiento es idéntico al de
> upstream.

| Fichero | Cambio |
|---|---|
| `src/pretix/static/pretixbase/img/pretix-logo.svg` | logo AΩGR de Azrahel sobre fondo navy `#16163F` (para cabeceras claras) |
| `src/pretix/static/pretixbase/img/pretix-logo-white.svg` | logo AΩGR transparente (para fondos oscuros) |
| `src/pretix/static/pretixbase/img/favicon*.{png,ico}` | emblema circular AΩGR sobre navy |
| `src/pretix/static/pretixbase/img/icons/*` | idem (apple-touch, android-chrome, mstile) |
| `src/pretix/static/pretixbase/email/thumb_simple_logo.png` | logo de cabecera de emails |
| `deployment/docker/pretix.bash` | **única excepción no-asset**: hace configurable la concurrencia de celery (`CELERY_CONCURRENCY`), igual que ya lo era la de gunicorn (`NUM_WORKERS`) |

Origen de los assets: `logo-azrahel.png` (1024×557), extraído de azraheldelmayor.com.
El emblema circular es el recorte `bbox (19, 18, 505, 539)` de ese PNG — mismo criterio que se usó en
el fork de Hi.Events, para que ambas plataformas se vean idénticas.

Paleta de marca: morado `#8617CE` · navy `#16163F` · dorado `#DBB56A` · tierra `#A37D66`.

---

## Lo que NO se toca (y por qué)

**El pie "powered by" no se modifica en código.** pretix ya trae la opción nativa, y la licencia
(AGPLv3 + términos adicionales §7, punto 2) permite reformularlo pero **no eliminarlo**.

Se configura desde el panel: *Admin global → Settings → License* →
- `poweredby_name` = `AMAI`
- `poweredby_url` = `https://amai.solutions`

Resultado en el pie: **"powered by AMAI based on pretix (source code)"**, con *pretix* enlazando a
https://pretix.eu — exactamente la forma que autoriza la licencia.

El enlace *source code* apunta a `/source`, que debe describir cómo obtener este repositorio.
Se rellena en *Settings → License → Source code instructions*.

**Colores, logo del organizador y diseño de la tienda** son configuración por evento/organizador
(*Settings → Shop design*), no código. No tiene sentido hornearlos en la imagen.

---

## Actualizar a una versión nueva de pretix

```bash
git fetch upstream --tags
git rebase v2026.X.0        # el diff son ~20 ficheros de assets, entra limpio
git push --force-with-lease origin azrahel
```

Después, redeploy en Dokploy.
