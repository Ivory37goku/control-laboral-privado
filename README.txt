MI CONTROL LABORAL — WEB APP
====================================

QUÉ ES
- Aplicación web adaptable a móvil y ordenador.
- Dashboard con gráfico de horas extra, resumen de dinero y accesos rápidos.
- Registros de horas extra, viajes, pagos recibidos y copias de seguridad.
- Edición de jornadas, viajes y pagos ya guardados.
- Fichaje 08:00–16:00 provisional al abrir la app después de las 16:00 en días laborables, si aún no hay registro.
- Estados revisado/provisional, comparación mensual y resumen anual de horas, viajes y cobros.
- Búsqueda y filtros por fechas, estado, categoría y concepto; avisos de jornadas incompletas y cierre mensual.
- Protección contra fichajes duplicados y confirmación explícita de pagos que parecen repetidos.
- Resúmenes mensuales imprimibles y descargables en CSV; exportación CSV para abrir con Excel.
- Asistente local gratuito que resume horas, viajes y pagos sin enviar datos fuera de la aplicación.
- Protección CSRF en formularios y validación de integridad de las copias descargables.
- Restauración de copias con validación y copia de seguridad previa automática.
- Contraseña de acceso mediante variable de entorno.

FICHAJE AUTOMÁTICO E INFORMES
- La app solo crea el fichaje automático cuando se abre después de las 16:00, de lunes a viernes, y no existe ya un registro para ese día.
- El fichaje automático queda provisional y se puede editar en Horas; la app no genera por sí sola una notificación a esa hora.
- En Informes elige un mes para ver jornadas, horas, viajes y cobros. El cierre avisa si quedan jornadas sin revisar o con entrada/salida incompleta. También hay comparación mensual y un resumen anual. Puedes descargar un CSV o imprimir/guardar la página como PDF.

DATOS PRECARGADOS
Se precargan una sola vez:
- Septiembre 2026: 18,5 h extra pendientes (registro agregado al 30/09; fecha aproximada).
- Octubre 2026: 21 h extra provisionales (al 26/10/2026).
- Dos viajes de octubre de 2026 a 200 € cada uno, pendientes (fechas aproximadas).
Las fechas aproximadas están anotadas para que puedas corregirlas. No se incluyen datos reales de Sesame.

PROBAR EN TU ORDENADOR
1. Instala Python 3.11 o posterior.
2. Descomprime el ZIP y abre una terminal dentro de la carpeta.
3. Crea entorno virtual (recomendado):
   python -m venv .venv
   Windows: .venv\Scripts\activate
   macOS/Linux: source .venv/bin/activate
4. Instala dependencias:
   pip install -r requirements.txt
5. Define variables de entorno antes de arrancar:
   Windows PowerShell:
     $env:APP_PASSWORD="una-contraseña-larga"
     $env:SECRET_KEY="una-clave-aleatoria-larga"
     python app.py
   macOS/Linux:
     export APP_PASSWORD='una-contraseña-larga'
     export SECRET_KEY='una-clave-aleatoria-larga'
     python app.py
6. Abre http://127.0.0.1:5000 en ese ordenador.
   Para móvil en la misma red Wi-Fi se puede configurar la app para escuchar en la LAN,
   pero esto no equivale a tener acceso desde cualquier lugar y no debe publicarse sin HTTPS.

MÓVIL + ORDENADOR CON LOS MISMOS DATOS
Para compartir los datos, la aplicación tiene que estar desplegada en un servidor al que
accedan ambos dispositivos. Este ZIP incluye render.yaml preparado para Render con disco
persistente. Pasos generales:
1. Sube el contenido a un repositorio privado de GitHub.
2. En Render crea un servicio nuevo desde ese repositorio y aplica render.yaml.
3. Define APP_PASSWORD con una contraseña larga.
4. Comprueba que el servicio usa disco persistente montado en /var/data.
5. Abre la URL HTTPS del servicio en móvil y ordenador e inicia sesión con la misma contraseña.

IMPORTANTE: No he desplegado la app por ti ni he creado un servidor/cuenta. El código está
preparado para desplegarse; compartir datos entre dispositivos solo funcionará después del
despliegue en un servidor persistente. No pongas el archivo de base de datos ni claves en
un repositorio público. Protege la cuenta de hosting y crea copias periódicas.

ASISTENTE LOCAL GRATUITO
- El asistente calcula resúmenes de jornadas, viajes y pagos a partir de los datos guardados.
- No necesita API key, no tiene coste de uso y no envía registros a un proveedor externo.
- Solo ofrece consultas y resúmenes; no puede crear, editar ni borrar registros.
- Para modificar datos, usa los formularios normales de la aplicación.

SEGURIDAD Y LIMITACIONES
- Usa HTTPS al desplegar.
- Cambia SECRET_KEY y configura APP_PASSWORD.
- En Render se activa la cookie de sesión segura; en uso local HTTP no se fuerza para que
  puedas iniciar sesión en localhost.
- El acceso actual es una contraseña compartida; no es un sistema multiusuario con cuentas
  independientes.
- SQLite está en el disco persistente del servidor; no es una sincronización entre dos copias.
- No se conecta automáticamente a Sesame. Importar datos requiere exportación o integración
  autorizada disponible por parte de tu empresa.
- La app es un registro personal, no determina por sí sola el cálculo legal de nóminas.

