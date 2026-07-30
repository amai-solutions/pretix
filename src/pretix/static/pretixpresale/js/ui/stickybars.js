/*global document, window*/
/*
 * Las dos barras fijas de movil. El CSS decide DONDE viven y en que tamanos
 * existen; este fichero solo decide CUANDO se ven.
 *
 * Las dos son copias de un control que ya esta en la pagina, nunca un control
 * nuevo: el "volver" clona el enlace de la cabecera y el boton de compra envia
 * el mismo formulario que el boton original, con el atributo `form`. Asi no
 * pueden desincronizarse del destino ni de las cantidades que haya elegido el
 * comprador, y si este fichero no llega a cargarse la pagina se queda como
 * estaba: los controles originales siguen ahi y siguen funcionando.
 */
(function () {
    "use strict";

    var FORM_ID = "cart-add-form";
    var CHECKOUT_FORM_ID = "cart-checkout-form";
    var UMBRAL_SCROLL = 8;  // px; por debajo de esto es temblor de dedo, no intencion

    function alEstarLista(fn) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", fn);
        } else {
            fn();
        }
    }

    /*
     * "Volver": se aparta al bajar y vuelve al subir. Arriba del todo no se
     * muestra nunca, porque ahi el enlace de verdad ya esta a la vista.
     */
    function montarVolver() {
        var original = document.querySelector("a.page-header-link-back");
        if (!original) {
            return;
        }

        var barra = document.createElement("div");
        barra.className = "sticky-back";
        var copia = original.cloneNode(true);
        copia.removeAttribute("id");
        barra.appendChild(copia);
        document.body.appendChild(barra);

        var ultimoY = window.pageYOffset;
        var pendiente = false;

        function evaluar() {
            pendiente = false;
            var y = window.pageYOffset;
            var delta = y - ultimoY;

            if (Math.abs(delta) < UMBRAL_SCROLL) {
                return;
            }
            // Cerca del principio el enlace original ya se ve: sobra la copia.
            var visible = delta < 0 && y > 120;
            barra.classList.toggle("is-visible", visible);
            ultimoY = y;
        }

        window.addEventListener("scroll", function () {
            if (!pendiente) {
                pendiente = true;
                window.requestAnimationFrame(evaluar);
            }
        }, {passive: true});
    }

    /*
     * "Comprar": aparece en cuanto el boton de verdad se va de la pantalla, sin
     * importar si el comprador aun no ha bajado hasta el o si ya lo ha pasado.
     * En los dos casos la accion deja de estar a un dedo de distancia, que es lo
     * unico que esta barra viene a arreglar.
     *
     * Cual es "el boton de verdad" depende de si ya hay carrito. Con la cesta
     * llena, la accion de la pagina ya no es anadir sino pagar: si la barra
     * siguiera ofreciendo "anadir al carrito" invitaria a meter una segunda
     * entrada sin querer justo cuando lo que toca es terminar. Por eso se mira
     * primero el boton de la caja del carrito y solo si no hay se cae al de
     * anadir. La etiqueta se copia del original, asi que tambien acierta cuando
     * pretix escribe "Continuar" en vez de "Proceder con la compra".
     */
    function montarComprar() {
        if (!("IntersectionObserver" in window)) {
            return;
        }

        var origen = null;
        var formulario = null;

        var pagar = document.querySelector("#" + CHECKOUT_FORM_ID + " button[type=submit]");
        if (pagar) {
            origen = pagar;
            formulario = CHECKOUT_FORM_ID;
        } else if (document.getElementById("btn-add-to-cart") && document.getElementById(FORM_ID)) {
            origen = document.getElementById("btn-add-to-cart");
            formulario = FORM_ID;
        }
        if (!origen) {
            return;
        }

        var barra = document.createElement("div");
        barra.className = "sticky-buy";

        var copia = document.createElement("button");
        copia.type = "submit";
        copia.className = "btn btn-block btn-primary btn-lg";
        copia.setAttribute("form", formulario);
        copia.innerHTML = origen.innerHTML;
        if (origen.hasAttribute("aria-label")) {
            copia.setAttribute("aria-label", origen.getAttribute("aria-label"));
        }
        barra.appendChild(copia);

        document.body.appendChild(barra);
        document.body.classList.add("has-sticky-buy");

        new window.IntersectionObserver(function (entradas) {
            entradas.forEach(function (entrada) {
                barra.classList.toggle("is-visible", !entrada.isIntersecting);
            });
        }, {threshold: 0}).observe(origen);
    }

    alEstarLista(function () {
        montarVolver();
        montarComprar();
    });
}());
